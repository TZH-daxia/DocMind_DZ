"""分析管线的 6 个节点实现。

1. render_document   解析文件，产出页面文本与页面图片
2. transcribe_pages  文本层不足的页面交给 VLM 逐字转写
3. classify_pages    主单/分单/舱单识别与页面分段（主单只登记、不抽取）
4. extract_documents 抽取多份分单 + 一份舱单
5. verify_consistency 分单与舱单逐字段核对
6. build_result      装配最终结果并落盘
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.model_call import ModelCallFailedError
from app.collector.locator import (
    locate_fields,
    locate_manifest_fields,
    read_handling_info,
    read_value_below_label,
)
from app.collector.parsers import ParserError, build_default_registry
from app.collector.segmenter import (
    PageKind,
    classify_pages,
    detect_master_bill,
    find_hawb_no,
    group_segments,
    normalize_doc_no,
)
from app.config import Settings
from app.schemas.documents import (
    AnalysisResult,
    ComparisonSummary,
    FieldCandidate,
    HouseBill,
    HouseBillComparison,
    ManifestDocument,
    ManifestEntry,
    MasterBillInfo,
    PageRecord,
    Segment,
    TotalsCheck,
)
from app.schemas.fields import FIELD_LABELS, HAWB_UNRECOGNIZED, HOUSE_FIELD_KEYS
from app.service.extraction import (
    ExtractionError,
    classify_pages_with_llm,
    extract_house_bill,
    extract_manifest,
    transcribe_page,
)
from app.service.extraction import (
    transcribe_house_zoom as transcribe_house_zoom_page,
)
from app.service.gates import ConcurrencyGates
from app.service.normalizers import normalize_mawb
from app.service.ports import normalize_port_fields, resolve_port
from app.service.review import build_review_items
from app.storage.file_store import FileStore
from app.storage.task_docs import (
    load_document,
    load_documents,
    save_document,
    save_documents,
)
from app.workflow.state import AnalysisState

logger = logging.getLogger(__name__)

# Office 文档要经 LibreOffice 转 PDF：单实例进程，必须串行（并发闸门 "office"）
OFFICE_SUFFIXES = (".doc", ".docx", ".xls", ".xlsx")

# 邮箱按 PDF 文本层校正：VLM 漏一个字母（lanshenglght / anshenglight）太常见，
# 邮箱又是唯一能无损比对的字段，直接用文本层兜底，不用再花一次模型调用。
EMAIL_KEYS: tuple[str, ...] = ("email_fhr_hawb", "email_shr_hawb")

# 舱单行 → 分单接口字段：存在舱单时以舱单为准，前端只渲染「分单」页签，
# 所以要把舱单行按这套映射转成分单字段（收发货人 20 项两边本就同名，直接透传）。
MANIFEST_TO_HOUSE_FIELDS: dict[str, str] = {
    "mawb": "mawb",
    "hawb_no": "hawb",
    "sfg": "sfg",
    "dest_port": "mdg",
    "hbh": "hbh",
    "hbrq": "hbrq",
    "pieces": "zdpiece",
    "weight": "zdweight",
    # 计费重量（jfweight）与申报价值不再由后端提供，统一留给前端人工填写，
    # 所以舱单的 chargeable_weight 不再映射到分单字段。
    "volume": "zdvolume",
    "pm": "pm",
}


def _bills_from_manifest(manifest: ManifestDocument) -> list[HouseBill]:
    """把舱单里的每一行（一个 HAWB）转成一份「分单」。

    以舱单为准时不抽分单页，分单数据全部来自舱单行：字段按
    `MANIFEST_TO_HOUSE_FIELDS` 映射到分单接口名，缺的字段留空（前端表单会显示为空）；
    坐标一并重映射，点字段时高亮落在舱单页而不是分单页。
    """

    bills: list[HouseBill] = []
    for index, entry in enumerate(manifest.entries, start=1):
        fields: dict[str, str | None] = {key: None for key in HOUSE_FIELD_KEYS}
        locations: dict[str, list[dict[str, Any]]] = {}
        for source_key, value in (entry.fields or {}).items():
            target = MANIFEST_TO_HOUSE_FIELDS.get(source_key, source_key)
            if target in fields:
                fields[target] = value
        for source_key, boxes in (entry.field_locations or {}).items():
            target = MANIFEST_TO_HOUSE_FIELDS.get(source_key, source_key)
            if target:
                locations[target] = boxes
        bills.append(
            HouseBill(
                bill_id=f"bill_{index:03d}",
                page_indexes=list(manifest.page_indexes),
                fields=fields,
                field_locations=locations,
                confidence=entry.confidence,
                warnings=[
                    *entry.warnings,
                    f"数据来自舱单第 {entry.line_no or index} 行",
                ],
                source="manifest",
            )
        )
    return bills


def _has_master_number(
    master_bills: list[Any], house_bills: list[Any], manifest: Any
) -> bool:
    """是否识别到主单号（MAWB）。

    主单号可能出现在三处，任一处有值就算识别到：
    - 分类阶段登记的主单页（`master_no`）；
    - 分单抽取结果里的 `mawb`（AWB 左上角那串）；
    - 舱单每行的 `mawb` 列。

    需求：**没有主单号就判定失败**（主单号是 ERP 入库的必需条件）。
    """

    for bill in master_bills or []:
        number = (
            bill.get("master_no")
            if isinstance(bill, dict)
            else getattr(bill, "master_no", None)
        )
        if str(number or "").strip():
            return True

    for bill in house_bills or []:
        fields = bill.fields if hasattr(bill, "fields") else (bill.get("fields") or {})
        if str((fields or {}).get("mawb") or "").strip():
            return True

    entries: list[Any] = []
    if manifest is not None:
        if hasattr(manifest, "entries"):
            entries = list(manifest.entries)
        elif isinstance(manifest, dict):
            entries = list(manifest.get("entries") or [])
    for entry in entries:
        fields = entry.fields if hasattr(entry, "fields") else (entry.get("fields") or {})
        if str((fields or {}).get("mawb") or "").strip():
            return True

    return False


def _is_degenerate_transcription(text: str) -> bool:
    """视觉转写是否"读废了"：大量竖线/空单元格、几乎没有有效内容。

    典型废结果：`416 | PVG | 52342905 | | | | | …`（十几 KB 全是分隔符）。
    """

    compact = re.sub(r"\s+", "", text or "")
    if len(compact) < 40:
        return True
    return compact.count("|") / len(compact) >= 0.6
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_SIMILARITY_THRESHOLD = 0.9

# 货物行右侧的计价列：Rate/Charge 与 Total 必须与计费重量同行、且在其右侧
CHARGE_COLUMN_KEYS: tuple[str, ...] = ("rate", "ratetotal")
# 同行判定：归一化坐标下的纵向容差（约 6pt / 842pt）
CHARGE_ROW_SLACK = 0.007

# 港口字段 → 版式栏位标签：抽取值与栏位对不上时按标签下方回读
PORT_LAYOUT_LABELS: tuple[tuple[str, str], ...] = (
    ("sfg", "AIRPORT OF DEPARTURE"),
    ("mdg", "AIRPORT OF DESTINATION"),
)
# 当事人联系方式：若定位落在 Handling Information 栏，说明取自 Notify/Agent 段
CONTACT_FIELD_KEYS: tuple[str, ...] = (
    "phone_fhr_hawb",
    "email_fhr_hawb",
    "fax_fhr_hawb",
    "phone_shr_hawb",
    "email_shr_hawb",
    "fax_shr_hawb",
)


def _compact_match_text(text: str) -> str:
    """把 VLM Markdown 与引用压成可比较的紧凑文本（去标记、空白、大小写）。"""

    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[|*#`>]+", " ", text)
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", "", text).lower()


def _candidate_page_text(pages: list[PageRecord], page_indexes: list[int]) -> str:
    """候选校验用的页面文本：局部精读与整页转写都算（抽取时局部优先）。"""

    indexes = set(page_indexes)
    parts: list[str] = []
    for page in pages:
        if page.index not in indexes:
            continue
        if page.zoom_text:
            parts.append(page.zoom_text)
        parts.append(page.text)
    return "\n".join(parts)


def _candidate_quotes(candidates: list[dict[str, Any]]) -> dict[str, str]:
    quotes: dict[str, str] = {}
    for item in candidates:
        for evidence in item.get("evidence") or []:
            quote = str(evidence.get("quote") or "").strip()
            if quote:
                quotes[str(item.get("field_key"))] = quote
                break
    return quotes


def _append_candidate_warning(item: dict[str, Any], message: str) -> None:
    warnings = item.setdefault("warnings", [])
    if message not in warnings:
        warnings.append(message)


# 港口字段的「原文 → 三字码」是既定归一规则，不算值异常，不进待审核
NORMALIZED_CANDIDATE_KEYS = frozenset({"sfg", "mdg"})


def _align_candidates(
    candidates: list[dict[str, Any]], fields: dict[str, str | None]
) -> list[dict[str, Any]]:
    """候选值与最终字段保持一致；归一化/版面修正造成的差异标待审核。"""

    for item in candidates:
        key = str(item.get("field_key") or "")
        original = item.get("value")
        if original not in (None, "") and not item.get("raw_value"):
            item["raw_value"] = str(original)
        final = fields.get(key)
        item["value"] = final
        if not final:
            item["status"] = "missing" if not item.get("raw_value") else "needs_review"
            continue
        if item.get("raw_value") and str(item["raw_value"]) != str(final):
            if key in NORMALIZED_CANDIDATE_KEYS:
                _append_candidate_warning(item, "港口已归一化为三字码")
                continue
            if key == "pm":
                _append_candidate_warning(
                    item, "品名已剔除 HS CODE/发票号等非品名内容"
                )
                continue
            if item.get("status") == "confirmed":
                item["status"] = "needs_review"
            _append_candidate_warning(item, "值与模型原文不一致（已按版面/规则修正）")
    return candidates


def _quote_probes(quote: str) -> list[str]:
    """把 quote 拆成可校验的探针：整串、省略号分段、较长 token。

    模型常在 quote 里用 `...` 连接栏位名和值，或把不同栏的文字拼在一起，
    整串在转写文本里天然不连续；只要一个片段能在转写文本里命中，就认为引用可信。
    """

    probes: list[str] = []
    compact = _compact_match_text(quote)
    if compact:
        probes.append(compact)
    for part in re.split(r"\.{2,}|…+|\||<br\s*/?>", quote, flags=re.IGNORECASE):
        candidate = _compact_match_text(part)
        if len(candidate) >= 6:
            probes.append(candidate)
    for token in re.split(r"[\s,;:()\[\]{}]+", quote):
        candidate = _compact_match_text(token)
        if len(candidate) >= 6:
            probes.append(candidate)
    return list(dict.fromkeys(sorted(probes, key=len, reverse=True)))


def _validate_candidates(
    pages: list[PageRecord],
    page_indexes: list[int],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验 evidence.quote 是否真的出现在转写文本里；不通过只降级，不丢值。"""

    # 兼容旧格式/模型漏输出：整单一条 quote 都没有时不逐字段降级，
    # 否则会把"模型没按新格式给引用"误判成所有字段都不可信
    quoted = sum(
        1
        for item in candidates
        if any((e.get("quote") or "").strip() for e in item.get("evidence") or [])
    )
    if quoted == 0:
        return candidates, ["模型未返回原文引用，字段级复核不可用"]
    page_text = _compact_match_text(_candidate_page_text(pages, page_indexes))
    missing_quotes = 0
    stale_quotes = 0
    for item in candidates:
        value = item.get("value")
        if not value:
            continue
        if str(value) == HAWB_UNRECOGNIZED:
            # 占位值不是真实分单号：标待审核，但不要算成「缺少原文引用」
            item["status"] = "needs_review"
            _append_candidate_warning(item, "分单号未识别，已填占位值")
            continue
        quote = ""
        for evidence in item.get("evidence") or []:
            quote = str(evidence.get("quote") or "").strip()
            if quote:
                break
        if not quote:
            missing_quotes += 1
            item["status"] = "needs_review"
            item.setdefault("warnings", []).append("缺少原文引用")
            continue
        if not any(probe in page_text for probe in _quote_probes(quote)):
            stale_quotes += 1
            item["status"] = "needs_review"
            _append_candidate_warning(item, "原文引用未在转写文本中找到")
        # 日期字段的引用必须是连续的一段：用 `...` 把两处日期拼起来时，
        # 引用本身就不等于页面原文，直接标待审核并说明原因。
        if item.get("field_key") == "hbrq" and re.search(r"\.{2,}|…", quote):
            item["status"] = "needs_review"
            _append_candidate_warning(item, "日期引用由多段拼接，请确认航班日期")
    warnings: list[str] = []
    if stale_quotes:
        warnings.append(
            f"{stale_quotes} 个字段的原文引用未在转写文本中找到，已转待审核"
        )
    if missing_quotes:
        warnings.append(f"{missing_quotes} 个字段缺少原文引用，已转待审核")
    return candidates, warnings


def _box_inside(box: dict[str, Any], block: dict[str, Any]) -> bool:
    """定位框中心是否落在给定区块内（判断值取自哪一栏）。"""

    if box.get("page") != block.get("page"):
        return False
    x, y, width, height = box["bbox"]
    bx, by, bw, bh = block["bbox"]
    center_x = x + width / 2
    center_y = y + height / 2
    return bx <= center_x <= bx + bw and by <= center_y <= by + bh


# AWB 页首的主单号：416|PVG|52331591 / 416 TSN 10799445 / 416-52331591
_MAWB_HEAD_RE = re.compile(r"\d{3}[\s\-|/]{0,3}[A-Z]{0,3}[\s\-|/]{0,3}\d{8}")
# 电话行里的数字串（+865125732098）形状与主单号相同，必须排除
_PHONE_LINE_RE = re.compile(r"\b(TE|TEL|FAX|MOBILE)\b|[+]", re.IGNORECASE)


_MAWB_DIGITS_RE = re.compile(r"\d{11}")


def _canonical_master_no(value: str | None) -> str | None:
    """主单号必须是「3 位航司代码 + 8 位序号」。

    分单号（SZA26100005）、发票号之类的也能凑出 11 位数字，不能当主单号参与
    单票多主单判定，否则会把一张 House AWB 误判成「第二个主单」。
    """

    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not _MAWB_DIGITS_RE.fullmatch(digits):
        return None
    return f"{digits[:3]}-{digits[3:]}"


def _head_master_nos(
    pages: list[PageRecord], segments: list[Segment]
) -> dict[str, list[int]]:
    """从 AWB 页页首扫主单号（只读已转写文本，不额外调模型）。

    单票模式用它发现「第二个主单号印在分单页左上角」的情况：这类页面整页被判为
    house_bill，不会出现在 master_bills 里，只看 master_bills 会漏判。
    """

    targets = {
        index
        for segment in segments
        if segment.kind in {"master_bill", "house_bill"}
        for index in segment.page_indexes
    }
    found: dict[str, list[int]] = {}
    for page in pages:
        if page.index not in targets or not page.text:
            continue
        lines = [line for line in page.text.splitlines() if line.strip()][:3]
        head = "\n".join(line for line in lines if not _PHONE_LINE_RE.search(line))
        for token in _MAWB_HEAD_RE.findall(head):
            number = normalize_mawb(token)
            if number and page.index not in found.get(number, []):
                found.setdefault(number, []).append(page.index)
    return found


# 品名栏里的非品名附加行：HS CODE、发票号、P.O. 等
HS_CODE_RE = re.compile(r"\bHS\s*CODE\s*[:：]?\s*[0-9A-Z.\-]+", re.IGNORECASE)
INVOICE_REF_RE = re.compile(r"\bIN[.。:：]?\s*[0-9]{6,}\b", re.IGNORECASE)
PO_REF_RE = re.compile(r"\bP\.?\s*O\.?\s*(?:NO\.?|#)?\s*[0-9A-Z\-]+", re.IGNORECASE)


def clean_commodity_description(value: str | None) -> tuple[str | None, bool]:
    """品名栏只保留货名/型号：剔除 HS CODE、发票号、P.O. 等附加行。"""

    if not value:
        return value, False
    cleaned = HS_CODE_RE.sub(" ", value)
    cleaned = INVOICE_REF_RE.sub(" ", cleaned)
    cleaned = PO_REF_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;|")
    return (cleaned or None), cleaned != value


def _drop_unreliable_charge_fields(
    fields: dict[str, str | None],
    locations: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """校验 Rate/Charge 与 Total 是否落在货物行对应列上，位置不对就置空。

    货物表格的这两列经常是空栏，模型会把下方费用区（Weight Charge、
    Total Prepaid 等）或品名栏（DIMS ... x 1）里的数字当成单价/总价。
    用定位结果兜底：不在计费重量右侧、或不同行，就视为空栏并清掉模型值。
    """

    # 锚点用制单重量（与 Rate/Total 同在货物行）；计费重量已不再抽取，不能再用它定位
    anchor_boxes = locations.get("zdweight") or []
    if not anchor_boxes:
        return []
    anchor = anchor_boxes[0]
    anchor_x, anchor_y, _anchor_w, anchor_h = anchor["bbox"]

    warnings: list[str] = []
    for key in CHARGE_COLUMN_KEYS:
        boxes = locations.get(key) or []
        if not boxes or not fields.get(key):
            continue
        box = boxes[0]
        x, y, _w, h = box["bbox"]
        same_page = box["page"] == anchor["page"]
        same_row = (
            y < anchor_y + anchor_h + CHARGE_ROW_SLACK
            and y + h > anchor_y - CHARGE_ROW_SLACK
        )
        if same_page and same_row and x > anchor_x:
            continue
        fields[key] = None
        locations.pop(key, None)
        warnings.append(f"{FIELD_LABELS.get(key, key)}栏为空，已置空")
    return warnings


class PipelineError(RuntimeError):
    """面向前端的业务错误（code + 中文 message）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Pipeline:
    def __init__(self, settings: Settings, store: FileStore, gateway: Any) -> None:
        self.settings = settings
        self.store = store
        self.gateway = gateway
        self.registry = build_default_registry()
        # 由 TaskService 注入共享闸门；独立使用（测试）时退回自带实例
        self.gates = ConcurrencyGates()

    # ------------------------------------------------------------------ 1
    async def render_document(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        cached = load_document(self.store, task_id)
        if cached.get("pages"):
            return {
                "pages": [page.model_dump() for page in cached["pages"]],
                "parser": cached.get("parser", ""),
                "warnings": cached.get("warnings", []),
                "page_boundaries": cached.get("page_boundaries", []),
                "files": cached.get("files", []),
            }

        files = state.get("files") or [
            {
                "name": state.get("source_name") or Path(state["uploaded_path"]).name,
                "path": state["uploaded_path"],
            }
        ]
        pages: list[PageRecord] = []
        boundaries: list[int] = []
        warnings: list[str] = []
        parser_names: list[str] = []

        for file_index, item in enumerate(files, start=1):
            source = Path(item["path"])
            parser = self.registry.get(source.suffix)
            await self._emit(
                state,
                "解析文档",
                f"使用 {parser.name} 解析器读取 {item['name']}"
                f"（{source.stat().st_size // 1024} KB）",
                8,
            )
            # 每个文件单独子目录，避免 page_001.png 互相覆盖
            out_dir = self.store.task_dir(task_id) / f"src{file_index:02d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                # 渲染/转换是同步阻塞调用：丢线程池，避免卡住事件循环；
                # Office 转换再加一层闸门，LibreOffice 单实例不能并行
                if source.suffix.lower() in OFFICE_SUFFIXES:
                    async with self.gates.acquire(
                        "office", self.settings.office_concurrency
                    ):
                        parsed = await asyncio.to_thread(
                            parser.parse, source, out_dir, self.settings
                        )
                else:
                    parsed = await asyncio.to_thread(
                        parser.parse, source, out_dir, self.settings
                    )
            except ParserError as exc:
                raise PipelineError(exc.code, f"{item['name']}：{exc.message}") from exc
            if not parsed.pages:
                warnings.append(f"{item['name']} 未解析出任何页面")
                continue
            boundaries.append(len(pages) + 1)
            for page in parsed.pages:
                pages.append(
                    PageRecord(
                        index=len(pages) + 1,
                        text=page.text,
                        text_source=page.text_source,  # type: ignore[arg-type]
                        image_rel=self.store.relative_path(page.image_path)
                        if page.image_path
                        else None,
                        char_count=len(page.text),
                        # 用落盘文件名（safe_filename 净化后），否则含空格/特殊字符的
                        # 原名与磁盘文件不一致，定位原图时会找不到 PDF
                        source_file=source.name,
                        page_in_file=page.index,
                    )
                )
            parser_names.append(parser.name)
            warnings.extend(f"{item['name']}：{text}" for text in parsed.warnings)

        if not pages:
            raise PipelineError("PARSE_FAILED", "文件未解析出任何页面")

        parser_label = "+".join(dict.fromkeys(parser_names))
        self._save_pages(
            task_id,
            pages,
            parser_label,
            warnings,
            transcribed=False,
            page_boundaries=boundaries,
            files=files,
        )
        await self._emit(
            state,
            "解析文档",
            f"解析完成：{len(files)} 个文件、{len(pages)} 页（{parser_label}）",
            22,
        )
        return {
            "pages": [page.model_dump() for page in pages],
            "parser": parser_label,
            "warnings": warnings,
            "page_boundaries": boundaries,
            "files": files,
        }

    # ------------------------------------------------------------------ 2
    async def transcribe_pages(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        pages = [PageRecord(**item) for item in state.get("pages") or []]
        cached = load_document(self.store, task_id)
        if cached.get("transcribed"):
            return {
                "pages": [page.model_dump() for page in cached.get("pages") or []],
                "warnings": cached.get("warnings", []),
                "page_boundaries": cached.get("page_boundaries", []),
                "files": cached.get("files", []),
            }

        # 所有页面都走视觉转写：不信任文本层（扫描件、字体乱码、表格错位），
        # 文本层只保留给「字段坐标定位」使用
        targets = [page for page in pages if page.image_rel]
        warnings: list[str] = []
        if targets:
            await self._emit(
                state,
                "图像转写",
                f"统一调用视觉模型逐字转写 {len(targets)} 页",
                28,
            )

        # 逐页转写并发度可配；实际还会再受模型总闸门 model_max_concurrency 限制
        semaphore = asyncio.Semaphore(self.settings.vision_page_concurrency)

        async def transcribe(page: PageRecord) -> None:
            async with semaphore:
                image_path = str(self.store.resolve_relative(page.image_rel or ""))
                try:
                    text = await transcribe_page(self.gateway, page, image_path)
                except Exception as exc:  # noqa: BLE001 - 单页失败不阻断整份文件
                    warnings.append(f"第 {page.index} 页图像转写失败：{exc}")
                    return
                raw = page.text.strip()
                if text and text.strip():
                    if _is_degenerate_transcription(text) and len(raw) >= 200:
                        # 视觉模型把整页读成一堆空单元格（"416 | PVG | 52342905 | | | …"）时，
                        # 下游分类会把它当成"上一页表格续页"，进而丢页/判错单据类型。
                        # 这类结果必须丢掉，回退到文本层（Office 转出的 PDF 文本层是干净的）。
                        warnings.append(
                            f"第 {page.index} 页视觉转写异常（有效内容过少），已改用文本层"
                        )
                        page.text = raw
                        page.text_source = "text_layer"
                        page.char_count = len(page.text)
                        return
                    page.text = text.strip()
                    page.text_source = "vlm"
                    page.char_count = len(page.text)

        if targets:
            await asyncio.gather(*(transcribe(page) for page in targets))

        empty_pages = [page.index for page in pages if not page.text.strip()]
        if empty_pages:
            warnings.append(
                "以下页面没有可用文本：" + "、".join(str(index) for index in empty_pages)
            )

        self._save_pages(
            task_id,
            pages,
            cached.get("parser", state.get("parser", "")),
            [*cached.get("warnings", []), *warnings],
            transcribed=True,
            page_boundaries=cached.get("page_boundaries", []),
            files=cached.get("files", []),
        )
        vlm_pages = sum(1 for page in targets if page.text_source == "vlm")
        await self._emit(
            state,
            "图像转写",
            f"视觉转写完成：{vlm_pages}/{len(pages)} 页",
            42,
            "warning" if empty_pages else "info",
        )
        return {
            "pages": [page.model_dump() for page in pages],
            "warnings": [*(state.get("warnings") or []), *warnings],
            "page_boundaries": cached.get("page_boundaries", []),
            "files": cached.get("files", []),
        }

    # ------------------------------------------------------------------ 2b
    async def transcribe_house_zoom(self, state: AnalysisState) -> dict[str, Any]:
        """局部精读：对分单页或舱单页做四象限小字特写转写。

        有舱单时以舱单为准（分单页不再抽取），所以这时只精读舱单页；
        没有舱单才精读分单页。舱单通常是整票缩排的表格，小字比分单更糊，
        同样需要放大精读，否则表格里的号/重量容易读错。

        整页转写保留在 page.text，局部精读写入 page.zoom_text，抽取时局部优先。
        转写 md 落盘便于人工对照「转写 vs 提取」；
        完成记录写在 transcription/zoom.json，断点续跑不会无限重试。
        """

        task_id = state["task_id"]
        pages = [PageRecord(**item) for item in state.get("pages") or []]
        segments = [Segment(**item) for item in state.get("segments") or []]
        kinds = {segment.kind for segment in segments}
        target_kind = "manifest" if "manifest" in kinds else "house_bill"
        doc_label = "舱单" if target_kind == "manifest" else "分单"
        target_indexes = sorted(
            {
                index
                for segment in segments
                if segment.kind == target_kind
                for index in segment.page_indexes
            }
        )
        if not target_indexes:
            return {}

        by_index = {page.index: page for page in pages}
        status_path = self.store.zoom_status_path(task_id)
        done = (self.store.read_json_or_none(status_path) or {}).get("pages") or {}
        pending = [
            by_index[index]
            for index in target_indexes
            if index in by_index and str(index) not in done
        ]
        if not pending:
            return {}

        await self._emit(
            state,
            f"{doc_label}局部精读",
            f"对 {len(pending)} 页{doc_label}做四象限放大转写",
            49,
        )
        warnings: list[str] = []
        semaphore = asyncio.Semaphore(self.settings.vision_page_concurrency)
        records: dict[str, dict[str, Any]] = dict(done)

        async def zoom(page: PageRecord) -> None:
            async with semaphore:
                try:
                    images = await asyncio.to_thread(
                        self._render_part_images, task_id, pages, page
                    )
                except Exception as exc:  # noqa: BLE001 - 单页切图失败不阻断
                    warnings.append(f"第 {page.index} 页局部切图失败：{exc}")
                    records[str(page.index)] = {"status": "failed"}
                    return
                if len(images) < 4:
                    # 图片直传等没有源 PDF 的场景没有可切的原图，跳过并记录
                    records[str(page.index)] = {"status": "skipped"}
                    return
                try:
                    text = await transcribe_house_zoom_page(
                        self.gateway, page, images, doc_label
                    )
                except Exception as exc:  # noqa: BLE001 - 单页失败不阻断
                    warnings.append(f"第 {page.index} 页局部精读失败：{exc}")
                    records[str(page.index)] = {"status": "failed"}
                    return
                if not (text or "").strip():
                    records[str(page.index)] = {"status": "empty"}
                    return
                page.zoom_text = text.strip()
                target = self.store.transcription_path(task_id, page.index, "_zoom")
                self.store.write_text_atomic(target, page.zoom_text)
                page.zoom_rel = self.store.relative_path(target)
                records[str(page.index)] = {
                    "status": "done",
                    "rel": page.zoom_rel,
                }

        await asyncio.gather(*(zoom(page) for page in pending))
        self.store.write_json_atomic(status_path, {"pages": records})
        merged_warnings = [*(state.get("warnings") or []), *warnings]
        self._save_pages(
            task_id,
            pages,
            state.get("parser", ""),
            merged_warnings,
            transcribed=True,
            page_boundaries=state.get("page_boundaries") or [],
            files=state.get("files") or [],
        )
        done_count = sum(
            1 for item in records.values() if item.get("status") == "done"
        )
        await self._emit(
            state,
            f"{doc_label}局部精读",
            f"局部精读完成：{done_count}/{len(target_indexes)} 页",
            50,
            "warning" if warnings else "info",
        )
        return {
            "pages": [page.model_dump() for page in pages],
            "warnings": merged_warnings,
        }

    def _render_part_images(
        self, task_id: str, pages: list[PageRecord], page: PageRecord
    ) -> list[bytes]:
        """把一页按左上/右上/左下/右下切成四块（12% 重叠，300dpi）。

        只在内存里返回 PNG 字节：局部切图是喂给 VLM 的中间产物，
        不需要落盘到任务目录，避免 data 层再堆一批临时图片。
        """

        pdf_path, page_map = self._source_context(task_id, pages, [page.index])
        if pdf_path is None or page.index not in page_map:
            return []
        import pymupdf

        with pymupdf.open(pdf_path) as pdf:
            page_no = page_map[page.index]
            if page_no < 1 or page_no > pdf.page_count:
                return []
            doc_page = pdf.load_page(page_no - 1)
            width = float(doc_page.rect.width)
            height = float(doc_page.rect.height)
            mid_x, mid_y = width / 2, height / 2
            overlap_x, overlap_y = width * 0.12, height * 0.12
            boxes = (
                (0.0, 0.0, mid_x + overlap_x, mid_y + overlap_y),
                (mid_x - overlap_x, 0.0, width, mid_y + overlap_y),
                (0.0, mid_y - overlap_y, mid_x + overlap_x, height),
                (mid_x - overlap_x, mid_y - overlap_y, width, height),
            )
            parts: list[bytes] = []
            for box in boxes:
                pixmap = doc_page.get_pixmap(clip=pymupdf.Rect(*box), dpi=300)
                parts.append(pixmap.tobytes("png"))
            return parts

    # ------------------------------------------------------------------ 3
    async def classify_pages(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        cached = load_document(self.store, task_id)
        if cached.get("segments"):
            cached_pages = [
                page.model_dump() for page in cached.get("pages") or []
            ]
            return {
                "segments": cached.get("segments", []),
                "master_bills": cached.get("master_bills", []),
                "warnings": cached.get("warnings", []),
                "pages": cached_pages or state.get("pages", []),
            }

        pages = [PageRecord(**item) for item in state.get("pages") or []]
        warnings: list[str] = list(state.get("warnings") or [])
        boundaries = set(state.get("page_boundaries") or [])

        rule_kinds = classify_pages(
            [(page.index, page.text) for page in pages], boundaries
        )
        unknown = [
            page
            for page, kind in zip(pages, rule_kinds, strict=False)
            if kind.kind is None and page.text.strip()
        ]
        llm_map: dict[int, dict[str, Any]] = {}
        if unknown:
            try:
                llm_map = await classify_pages_with_llm(self.gateway, unknown)
            except (ModelCallFailedError, ExtractionError) as exc:
                warnings.append(f"模型页面分类失败，仅使用规则判定：{exc}")

        merged: list[PageKind] = []
        for page, rule in zip(pages, rule_kinds, strict=False):
            if rule.kind is not None:
                merged.append(rule)
                continue
            info = llm_map.get(page.index)
            if info:
                merged.append(
                    PageKind(
                        index=page.index,
                        kind=info["kind"],
                        doc_no=normalize_doc_no(info.get("doc_no")),
                        continuation=bool(info.get("continuation")),
                        confidence=float(info.get("confidence", 0.6)),
                        evidence=[info["reason"]] if info.get("reason") else ["模型分类"],
                        source="llm",
                    )
                )
            else:
                merged.append(
                    PageKind(
                        index=page.index,
                        kind="other",
                        confidence=0.0,
                        evidence=["规则与模型均无法判定"],
                        source="rules",
                    )
                )

        drafts = group_segments(merged, boundaries)
        segments: list[Segment] = []
        master_bills: list[MasterBillInfo] = []
        for position, draft in enumerate(drafts, start=1):
            segment_id = f"seg_{position:03d}"
            segments.append(
                Segment(
                    segment_id=segment_id,
                    kind=draft.kind,
                    label=draft.label,
                    page_indexes=draft.page_indexes,
                    doc_no=draft.doc_no,
                    confidence=round(draft.confidence, 3),
                    evidence=draft.evidence,
                )
            )
            if draft.kind == "master_bill":
                text = "\n".join(
                    page.text for page in pages if page.index in draft.page_indexes
                )
                signal = detect_master_bill(text)
                master_bills.append(
                    MasterBillInfo(
                        segment_id=segment_id,
                        page_indexes=draft.page_indexes,
                        master_no=signal.master_no or draft.doc_no,
                        confidence=round(max(signal.confidence, draft.confidence), 3),
                        evidence=signal.evidence or draft.evidence,
                    )
                )
            elif draft.kind == "house_bill" and not draft.doc_no:
                text = "\n".join(
                    page.text for page in pages if page.index in draft.page_indexes
                )
                if hawb_no := find_hawb_no(text):
                    segments[-1].doc_no = hawb_no

        # 单票模式：多个文件必须属于同一个主单；识别出多个主单直接判失败。
        # 判定看两处：主单段的 master_no + AWB 页首扫到的主单号。
        # 只看 master_bills 会漏掉「第二个主单号印在分单页左上角」的情况（整页判 house_bill）。
        if state.get("mode") == "single":
            scanned = _head_master_nos(pages, segments)
            # 主单号统一成 416-52342905 这种规范写法再比对：
            # 模型有时回 41652342905、有时回带连字符的，直接比集合会凭空多出一个主单。
            numbers = {
                canonical
                for bill in master_bills
                if (canonical := _canonical_master_no(bill.master_no))
            }
            numbers |= {
                canonical
                for number in scanned
                if (canonical := _canonical_master_no(number))
            }
            multiple = len(numbers) > 1 or (len(master_bills) > 1 and not numbers)
            if multiple:
                # 每个主单号都列出来（页首扫到的带上页码），否则用户只看到一个号，
                # 会以为「同一个主单怎么算成两个」。
                labels = "、".join(
                    f"{number}（第{'/'.join(map(str, scanned[number]))}页）"
                    if number in scanned
                    else number
                    for number in sorted(numbers)
                ) or "、".join(
                    f"第 {index} 份" for index in range(1, len(master_bills) + 1)
                )
                raise PipelineError(
                    "SINGLE_TICKET_MULTIPLE_MASTER",
                    f"单票模式只允许一个主单，当前识别到 {len(numbers) or len(master_bills)} 个主单"
                    f"（{labels}），请改用批量识别",
                )

        # 页面上的分类结果回写，前端可逐页查看判定依据
        kind_by_page = {kind.index: kind for kind in merged}
        for page in pages:
            kind = kind_by_page.get(page.index)
            if kind:
                page.kind = kind.kind
                page.kind_confidence = kind.confidence
                page.kind_evidence = kind.evidence

        house_count = sum(1 for item in segments if item.kind == "house_bill")
        manifest_pages = sum(
            len(item.page_indexes) for item in segments if item.kind == "manifest"
        )
        if house_count == 0:
            if master_bills and not manifest_pages:
                warnings.append(
                    "文件只包含主单，已跳过字段抽取；请补充分单（HAWB）与舱单后再提交"
                )
                hint = f"主单 {len(master_bills)} 份，未发现分单与舱单，已跳过字段抽取"
            elif master_bills and manifest_pages:
                warnings.append(
                    "文件只包含主单与舱单：主单只登记不抽取，将只抽取舱单字段"
                )
                hint = (
                    f"主单 {len(master_bills)} 份、舱单 {manifest_pages} 页，"
                    "未发现分单，仅抽取舱单字段"
                )
            elif manifest_pages:
                warnings.append(
                    "文件只包含舱单，将只抽取舱单字段；请补充分单（HAWB）后再提交"
                )
                hint = f"舱单 {manifest_pages} 页，未发现分单，仅抽取舱单字段"
            else:
                self._save_segments(task_id, segments, master_bills, warnings, pages)
                raise PipelineError(
                    "NO_DOCUMENT",
                    "未识别到主单、分单或舱单，请上传正确的单证文件",
                )
            self._save_segments(task_id, segments, master_bills, warnings, pages)
            await self._emit(
                state,
                "识别主单/分单/舱单",
                hint,
                52,
                "warning",
            )
            return {
                "segments": [item.model_dump() for item in segments],
                "master_bills": [item.model_dump() for item in master_bills],
                "warnings": warnings,
                "pages": [page.model_dump() for page in pages],
            }
        if not master_bills:
            warnings.append("未识别到主单页面，已对所有识别到的分单执行抽取")
        if manifest_pages == 0:
            warnings.append("未识别到舱单页面，分单将无法执行一致性核对")

        self._save_segments(task_id, segments, master_bills, warnings, pages)
        await self._emit(
            state,
            "识别主单/分单/舱单",
            f"主单 {len(master_bills)} 份（已跳过抽取）、分单 {house_count} 份、"
            f"舱单 {manifest_pages} 页",
            52,
            "info" if master_bills and manifest_pages else "warning",
        )
        return {
            "segments": [item.model_dump() for item in segments],
            "master_bills": [item.model_dump() for item in master_bills],
            "warnings": warnings,
            "pages": [page.model_dump() for page in pages],
        }

    # ------------------------------------------------------------------ 4
    async def extract_documents(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        if self.store.documents_path(task_id).exists():
            house_bills, manifest, cached_warnings = load_documents(
                self.store, task_id
            )
            return {
                "house_bills": house_bills,
                "manifest": manifest,
                "warnings": [*state.get("warnings", []), *cached_warnings],
            }

        pages = [PageRecord(**item) for item in state.get("pages") or []]
        segments = [Segment(**item) for item in state.get("segments") or []]
        warnings: list[str] = list(state.get("warnings") or [])

        house_segments = [item for item in segments if item.kind == "house_bill"]
        manifest_segments = [item for item in segments if item.kind == "manifest"]
        # 以舱单为准：有舱单时不再单独抽分单页（否则会得到两套互相矛盾的数据）
        if manifest_segments:
            house_segments = []

        total = len(house_segments) + (1 if manifest_segments else 0)
        finished = 0
        semaphore = asyncio.Semaphore(self.settings.house_bill_concurrency)

        async def run_house(index: int, segment: Segment) -> HouseBill:
            nonlocal finished
            async with semaphore:
                bill_no = segment.doc_no or f"第 {index} 份分单"
                await self._emit(
                    state,
                    "抽取分单或舱单字段",
                    f"正在抽取分单 {bill_no}（第 {index}/{len(house_segments)} 份）",
                    55 + int(25 * finished / max(total, 1)),
                )
                try:
                    result = await extract_house_bill(
                        self.gateway, pages, segment.page_indexes
                    )
                    fields = result["fields"]
                    # 港口归一前先记下页面原文：定位时要用原文（如 SHANGHAI PUDONG...），
                    # 用归一后的三字码（PVG）会在页面上搜到别处的大字代码
                    field_sources = {
                        key: str(fields.get(key)).strip()
                        for key in ("sfg", "mdg")
                        if fields.get(key)
                    }
                    # 港口只接受三字码：确定不了就留空，原文由候选的 raw_value 保留
                    normalize_port_fields(fields)
                    bill_warnings = result["warnings"]
                    confidence = result["confidence"]
                    candidates = result.get("candidates") or []
                    # 品名栏常把 HS CODE/发票号一起带出来，这里确定性剔除
                    pm_cleaned, pm_changed = clean_commodity_description(
                        fields.get("pm")
                    )
                    if pm_changed:
                        fields["pm"] = pm_cleaned
                        bill_warnings.append(
                            "品名栏剔除了 HS CODE/发票号等非品名内容"
                        )
                except (ModelCallFailedError, ExtractionError) as exc:
                    return HouseBill(
                        bill_id=f"bill_{index:03d}",
                        page_indexes=segment.page_indexes,
                        fields={},
                        confidence=0.0,
                        warnings=[f"分单抽取失败：{exc}"],
                    )
                finally:
                    finished += 1
                # 候选值与最终字段对齐（港口归一化后可能被清空，原文留在 raw_value）
                candidates = _align_candidates(candidates, fields)
                # quote 必须能在转写文本里找到；找不到只降级为待审核，不丢值
                candidates, quote_warnings = _validate_candidates(
                    pages, segment.page_indexes, candidates
                )
                bill_warnings = [*bill_warnings, *quote_warnings]
                # 邮箱用 PDF 文本层兜底：VLM 常漏一个字母（lanshenglght / anshenglight），
                # 趁定位前改对，高亮框才能落到真实位置
                pdf_path, page_map = self._source_context(
                    task_id, pages, segment.page_indexes
                )
                bill_warnings = [
                    *bill_warnings,
                    *self._correct_emails(fields, pdf_path, page_map),
                ]
                field_locations = self._locate_segment_fields(
                    task_id,
                    pages,
                    segment.page_indexes,
                    fields,
                    field_sources,
                    _candidate_quotes(candidates),
                )
                # 计价列靠版面位置兜底：空栏时模型容易把别处的数字填进来
                bill_warnings = [
                    *bill_warnings,
                    *_drop_unreliable_charge_fields(fields, field_locations),
                ]
                # 港口抽错（如把运单号前缀当始发港）时按版式栏位回读纠正
                bill_warnings.extend(
                    self._fix_ports_by_layout(
                        task_id,
                        pages,
                        segment.page_indexes,
                        fields,
                        field_sources,
                        field_locations,
                    )
                )
                # Handling Information 漏抽时按版面补全；联系方式取自该栏则清空
                bill_warnings.extend(
                    self._apply_handling_info(
                        task_id, pages, segment.page_indexes, fields, field_locations
                    )
                )
                # 港口/联系方式修正后再同步一次候选值
                candidates = _align_candidates(candidates, fields)
                return HouseBill(
                    bill_id=f"bill_{index:03d}",
                    page_indexes=segment.page_indexes,
                    fields=fields,
                    field_locations=field_locations,
                    field_sources=field_sources,
                    candidates=[FieldCandidate(**item) for item in candidates],
                    confidence=confidence,
                    warnings=bill_warnings,
                    source_excerpt=self._excerpt(pages, segment.page_indexes),
                )

        house_bills = await asyncio.gather(
            *(run_house(index, segment) for index, segment in enumerate(house_segments, start=1))
        )

        failed = [bill for bill in house_bills if not bill.fields]
        if house_bills and len(failed) == len(house_bills):
            raise PipelineError(
                "MODEL_CALL_FAILED",
                "所有分单抽取均失败，请检查模型配置或稍后重试",
            )
        for bill in failed:
            warnings.append(f"{bill.bill_id} 抽取失败，字段留空")

        manifest_document: ManifestDocument | None = None
        if manifest_segments:
            page_indexes = [
                index for segment in manifest_segments for index in segment.page_indexes
            ]
            await self._emit(
                state, "抽取分单或舱单字段", "正在抽取舱单表格", 78
            )
            try:
                result = await extract_manifest(self.gateway, pages, page_indexes)
                # 舱单每行也做一次邮箱校正（只读一次 PDF 文本层）
                pdf_path, page_map = self._source_context(
                    task_id, pages, page_indexes
                )
                entries = []
                for position, entry in enumerate(result["entries"], start=1):
                    entry_fields = entry["fields"]
                    entry_warnings = [
                        *entry["warnings"],
                        *self._correct_emails(entry_fields, pdf_path, page_map),
                    ]
                    entries.append(
                        ManifestEntry(
                            entry_id=f"mft_{position:04d}",
                            line_no=entry["line_no"] or position,
                            page_indexes=page_indexes,
                            fields=entry_fields,
                            field_locations=self._locate_manifest_fields(
                                task_id, pages, page_indexes, entry_fields
                            ),
                            confidence=entry["confidence"],
                            warnings=entry_warnings,
                        )
                    )
                manifest_document = ManifestDocument(
                    segment_id=manifest_segments[0].segment_id,
                    page_indexes=page_indexes,
                    entries=entries,
                    warnings=result["warnings"],
                )
                if not entries:
                    warnings.append("舱单未抽取出任何数据行")
            except (ModelCallFailedError, ExtractionError) as exc:
                warning = f"舱单抽取失败，无法核对：{exc}"
                warnings.append(warning)
                await self._emit(
                    state, "抽取分单或舱单字段", warning, 82, "error"
                )
        if manifest_document is not None:
            # 以舱单为准：每一行转成一份分单，前端只渲染分单页签
            house_bills = _bills_from_manifest(manifest_document)
            warnings.append(
                f"已按舱单生成 {len(house_bills)} 份分单（存在舱单时不使用分单页数据）"
            )

        # 需求：没有主单号直接判定失败（主单号是 ERP 入库的必需条件）
        if not _has_master_number(
            state.get("master_bills") or [], house_bills, manifest_document
        ):
            raise PipelineError(
                "NO_MAWB",
                "未识别到主单号（MAWB），无法生成分单；请确认上传的是空运单证",
            )

        house_payload = [bill.model_dump() for bill in house_bills]
        manifest_payload = (
            manifest_document.model_dump() if manifest_document is not None else None
        )
        # 分单/舱单写 documents.json，候选写 candidates.json（同一事实只存一份）
        save_documents(self.store, task_id, house_bills, manifest_document, warnings)
        manifest_lines = len(manifest_document.entries) if manifest_document else 0
        if manifest_document is not None:
            summary_text = (
                f"抽取完成：舱单 {manifest_lines} 行，已转换为 {len(house_bills)} 份分单"
            )
        else:
            summary_text = f"抽取完成：分单 {len(house_bills)} 份"
        await self._emit(
            state,
            "抽取分单或舱单字段",
            summary_text,
            84,
        )
        return {
            "house_bills": house_payload,
            "manifest": manifest_payload,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ 5
    async def verify_consistency(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        comparisons_path = self.store.comparisons_path(task_id)
        cached = self.store.read_json_or_none(comparisons_path)
        if cached:
            return {
                "comparisons": cached.get("comparisons", []),
                "totals_checks": cached.get("totals_checks", []),
                "summary": cached.get("summary", {}),
                "warnings": [*state.get("warnings", []), *cached.get("warnings", [])],
            }

        # 需求变更：不再做分单/舱单一致性核对。
        # 有舱单时「分单」本身就是舱单行转换来的，两侧是同一份数据，比对恒为一致；
        # 只有分单时也没有另一侧可比。仍然落一份空 comparisons.json，
        # 保证结果结构（前端摘要/页签）与断点续跑判定稳定。
        house_bills = list(state.get("house_bills") or [])
        # 舱单行数仍要计数：前端的 manifest_entry_count 取自这里（canView / 摘要都用）
        manifest_payload = state.get("manifest") or {}
        manifest_entries = manifest_payload.get("entries") or []
        empty_summary = ComparisonSummary(
            total_house_bills=len(house_bills),
            total_manifest_entries=len(manifest_entries),
        )
        payload = {
            "comparisons": [],
            "totals_checks": [],
            "summary": empty_summary.model_dump(),
            "warnings": [],
        }
        self.store.write_json_atomic(comparisons_path, payload)
        await self._emit(
            state,
            "核对分单与舱单",
            "已取消分单/舱单一致性核对",
            92,
            "info",
            phase="skipped",
        )
        return {
            "comparisons": [],
            "totals_checks": [],
            "summary": payload["summary"],
            "warnings": list(state.get("warnings") or []),
        }

    # ------------------------------------------------------------------ 6
    async def build_result(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        # 只有主单（既无分单也无舱单）时图会整段跳过抽取与核对，
        # 这里补"已跳过"事件供前端展示；只有舱单/只有分单的情况由核对节点自己上报。
        kinds = {item.get("kind") for item in state.get("segments") or []}
        if "house_bill" not in kinds and "manifest" not in kinds:
            for label in ("抽取分单或舱单字段", "核对分单与舱单"):
                await self._emit(state, label, "无分单与舱单，已跳过", 0, phase="skipped")
        pages = [PageRecord(**item) for item in state.get("pages") or []]
        house_bills = [HouseBill(**item) for item in state.get("house_bills") or []]
        # 只有主单（或主单页读不出号）时不会经过抽取节点，这里兜住同一条规则：
        # 没有主单号就是失败，避免「处理完成但什么都用不了」。
        if not _has_master_number(
            state.get("master_bills") or [], house_bills, state.get("manifest")
        ):
            raise PipelineError(
                "NO_MAWB",
                "未识别到主单号（MAWB），无法生成分单；请确认上传的是空运单证",
            )
        # 舱单明细不进结果：结果里只保留转换后的分单；回查见 documents.json
        comparisons = [
            HouseBillComparison(**item) for item in state.get("comparisons") or []
        ]
        totals_checks = [
            TotalsCheck(**item) for item in state.get("totals_checks") or []
        ]
        review_items = build_review_items(
            house_bills,
            self.settings.review_confidence_threshold,
        )
        result = AnalysisResult(
            task_id=task_id,
            source_name=state.get("source_name", ""),
            status="succeeded",
            created_at=_parse_datetime(state.get("created_at")),
            finished_at=datetime.now().astimezone(),
            elapsed_seconds=_elapsed(state.get("created_at")),
            page_count=len(pages),
            parser=state.get("parser", ""),
            pages=pages,
            segments=[Segment(**item) for item in state.get("segments") or []],
            master_bills=[MasterBillInfo(**item) for item in state.get("master_bills") or []],
            house_bills=house_bills,
            comparisons=comparisons,
            totals_checks=totals_checks,
            summary=ComparisonSummary(**state.get("summary") or {}),
            review_items=review_items,
            warnings=_unique(state.get("warnings") or []),
        )
        payload = result.model_dump(mode="json")
        # 候选只在 candidates.json 存一份；results 是交付物，不再内嵌候选
        for bill in payload.get("house_bills") or []:
            bill.pop("candidates", None)
        self.store.write_json_atomic(self.store.result_path(task_id), payload)
        await self._emit(
            state,
            "生成结果",
            f"任务完成，共 {result.summary.total_house_bills} 份分单、"
            f"{result.summary.inconsistent} 份存在字段差异",
            100,
        )
        return {"result": payload}

    # ------------------------------------------------------------------ 工具
    async def _emit(
        self,
        state: AnalysisState,
        step: str,
        message: str,
        progress: int,
        level: str = "info",
        *,
        phase: str = "",
        duration_ms: int | None = None,
    ) -> None:
        reporter = state.get("reporter")
        if reporter is not None:
            await reporter.emit(
                step, message, progress, level, phase=phase, duration_ms=duration_ms  # type: ignore[arg-type]
            )

    def _save_pages(
        self,
        task_id: str,
        pages: list[PageRecord],
        parser: str,
        warnings: list[str],
        *,
        transcribed: bool,
        page_boundaries: list[int] | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> None:
        save_document(
            self.store,
            task_id,
            pages,
            parser,
            warnings,
            transcribed=transcribed,
            page_boundaries=page_boundaries,
            files=files,
        )

    def _save_segments(
        self,
        task_id: str,
        segments: list[Segment],
        master_bills: list[MasterBillInfo],
        warnings: list[str],
        pages: list[PageRecord],
    ) -> None:
        # 分类结果写回同一个 document.json；warnings 与已有值合并，不覆盖前序节点
        existing = (
            self.store.read_json_or_none(self.store.document_path(task_id)) or {}
        )
        merged = list(
            dict.fromkeys([*(existing.get("warnings") or []), *warnings])
        )
        save_document(
            self.store,
            task_id,
            pages,
            warnings=merged,
            transcribed=True,
            segments=[item.model_dump() for item in segments],
            master_bills=[item.model_dump() for item in master_bills],
        )

    def _resolve_source_pdf(self, task_id: str, source_file: str | None) -> Path | None:
        """定位源文件对应的 PDF：原件是 PDF 直接用，Office 用转换后的 PDF。"""

        name = Path(source_file or "").name
        if not name:
            return None
        task_dir = self.store.task_dir(task_id)
        original = task_dir / name
        if original.suffix.lower() == ".pdf" and original.exists():
            return original
        stem = Path(name).stem
        for sub_dir in sorted(task_dir.glob("src*")):
            converted = sub_dir / f"{stem}.pdf"
            if converted.exists():
                return converted
        return None

    def _source_context(
        self, task_id: str, pages: list[PageRecord], page_indexes: list[int]
    ) -> tuple[Path | None, dict[int, int]]:
        """分单对应的源 PDF 与「全局页 index → PDF 页号」映射。"""

        by_index = {page.index: page for page in pages}
        first = by_index.get(page_indexes[0]) if page_indexes else None
        if first is None:
            return None, {}
        pdf_path = self._resolve_source_pdf(task_id, first.source_file)
        if pdf_path is None:
            return None, {}
        page_map: dict[int, int] = {}
        for index in page_indexes:
            page = by_index.get(index)
            if page is None or page.source_file != first.source_file:
                continue
            page_map[index] = page.page_in_file or index
        return pdf_path, page_map

    def _locate_segment_fields(
        self,
        task_id: str,
        pages: list[PageRecord],
        page_indexes: list[int],
        fields: dict[str, str | None],
        sources: dict[str, str] | None = None,
        quotes: dict[str, str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """在分单所在 PDF 中定位字段值，供前端点击字段时高亮。

        quotes 来自候选层的 evidence：整条引用先在页面上找锚点，值定位时优先在锚点附近取值，
        值定位失败退化为引用位置。比只用版式标签更能区分同名值的不同栏位。
        """

        pdf_path, page_map = self._source_context(task_id, pages, page_indexes)
        if pdf_path is None:
            return {}
        # 定位优先用页面原文（港口归一成三字码后，原文才是页面上真实存在的内容）
        lookup = {
            key: (sources or {}).get(key) or value
            for key, value in fields.items()
        }
        return locate_fields(pdf_path, page_map, lookup, quotes=quotes)

    def _fix_ports_by_layout(
        self,
        task_id: str,
        pages: list[PageRecord],
        page_indexes: list[int],
        fields: dict[str, str | None],
        sources: dict[str, str],
        locations: dict[str, list[dict[str, Any]]],
    ) -> list[str]:
        """港口定位不到时按版式栏位回读纠正（如把运单号前缀 TSN 当成始发港）。"""

        pending = [
            (key, label) for key, label in PORT_LAYOUT_LABELS if not locations.get(key)
        ]
        if not pending:
            return []
        pdf_path, page_map = self._source_context(task_id, pages, page_indexes)
        if pdf_path is None:
            return []
        warnings: list[str] = []
        for key, label in pending:
            raw = read_value_below_label(pdf_path, page_map, label)
            code, _ = resolve_port(raw)
            if not code or code == fields.get(key):
                continue
            fields[key] = code
            sources[key] = raw or code
            locations.update(
                locate_fields(pdf_path, page_map, {key: sources[key]})
            )
            warnings.append(f"{FIELD_LABELS.get(key, key)}按版面纠正为 {code}")
        return warnings

    def _apply_handling_info(
        self,
        task_id: str,
        pages: list[PageRecord],
        page_indexes: list[int],
        fields: dict[str, str | None],
        locations: dict[str, list[dict[str, Any]]],
    ) -> list[str]:
        """Handling Information 漏抽时按版面补全；联系方式若取自该栏则清空。"""

        pdf_path, page_map = self._source_context(task_id, pages, page_indexes)
        if pdf_path is None:
            return []
        text, block = read_handling_info(pdf_path, page_map)
        warnings: list[str] = []
        if text and not str(fields.get("handlinginfo") or "").strip():
            fields["handlinginfo"] = text
            warnings.append("Handling Information 按版面补全")
        if block is None:
            return warnings
        for key in CONTACT_FIELD_KEYS:
            boxes = locations.get(key) or []
            if not boxes or not _box_inside(boxes[0], block):
                continue
            fields[key] = None
            locations.pop(key, None)
            warnings.append(
                f"{FIELD_LABELS.get(key, key)}位于 Handling Information 栏，已清空"
            )
        return warnings

    def _correct_emails(
        self,
        fields: dict[str, str | None],
        pdf_path: Path | None,
        page_map: dict[int, int],
    ) -> list[str]:
        """用 PDF 文本层校正收/发货人邮箱，返回告警文案。

        只修「几乎就是同一个串」的情况：同 TLD + 相似度 ≥0.9。
        这样 `lanshenglght.com` 会改回 `lanshenglight.com`，
        而「页面里确实没有这个邮箱」时保持原值，不瞎猜。
        """

        if pdf_path is None or not page_map:
            return []
        if not any(str(fields.get(key) or "").strip() for key in EMAIL_KEYS):
            return []

        try:
            import pymupdf

            found: list[str] = []
            with pymupdf.open(pdf_path) as pdf:
                for page_no in sorted(set(page_map.values())):
                    if 1 <= page_no <= pdf.page_count:
                        found.extend(
                            EMAIL_PATTERN.findall(
                                pdf.load_page(page_no - 1).get_text()
                            )
                        )
        except Exception as exc:  # noqa: BLE001 - 校正失败不影响主流程
            logger.warning("邮箱校正读取文本层失败（%s）：%s", pdf_path.name, exc)
            return []
        if not found:
            return []

        pool = {item.lower() for item in found}
        warnings: list[str] = []
        for key in EMAIL_KEYS:
            value = str(fields.get(key) or "").strip()
            if not value or "@" not in value or value.lower() in pool:
                continue
            suffix = value.rsplit(".", 1)[-1].lower()
            best: str | None = None
            best_score = 0.0
            for candidate in found:
                if candidate.rsplit(".", 1)[-1].lower() != suffix:
                    continue
                score = difflib.SequenceMatcher(
                    None, value.lower(), candidate.lower()
                ).ratio()
                if score > best_score:
                    best, best_score = candidate, score
            if best is None or best_score < EMAIL_SIMILARITY_THRESHOLD:
                continue
            fields[key] = best
            role = "发货人" if "_fhr_" in key else "收货人"
            warnings.append(f"{role}邮箱按版面文本校正：{value} → {best}")
        return warnings

    def _locate_manifest_fields(
        self,
        task_id: str,
        pages: list[PageRecord],
        page_indexes: list[int],
        fields: dict[str, str | None],
    ) -> dict[str, list[dict[str, Any]]]:
        """舱单行定位：以分单号锚定行，再在行内定位其他字段。"""

        by_index = {page.index: page for page in pages}
        first = by_index.get(page_indexes[0]) if page_indexes else None
        if first is None:
            return {}
        pdf_path = self._resolve_source_pdf(task_id, first.source_file)
        if pdf_path is None:
            return {}
        page_map = {
            index: by_index[index].page_in_file
            for index in page_indexes
            if index in by_index and by_index[index].source_file == first.source_file
        }
        return locate_manifest_fields(
            pdf_path, page_map, fields, anchor=fields.get("hawb_no")
        )

    @staticmethod
    def _excerpt(pages: list[PageRecord], indexes: list[int]) -> str:
        text = "\n".join(page.text for page in pages if page.index in indexes)
        return text[:800]


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now().astimezone()


def _elapsed(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        delta = datetime.now().astimezone() - datetime.fromisoformat(value)
        return round(delta.total_seconds(), 2)
    except ValueError:
        return 0.0
