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
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.model_call import ModelCallFailedError
from app.collector.locator import locate_fields, locate_manifest_fields
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
    HouseBill,
    HouseBillComparison,
    ManifestDocument,
    ManifestEntry,
    ManifestMaster,
    MasterBillInfo,
    PageRecord,
    Segment,
    TotalsCheck,
)
from app.schemas.fields import FIELD_LABELS
from app.service.consistency import compare, compare_totals
from app.service.extraction import (
    ExtractionError,
    classify_pages_with_llm,
    extract_house_bill,
    extract_manifest,
    transcribe_page,
)
from app.service.gates import ConcurrencyGates
from app.service.ports import normalize_port_fields
from app.service.review import build_review_items
from app.storage.file_store import FileStore
from app.workflow.state import AnalysisState

logger = logging.getLogger(__name__)

# Office 文档要经 LibreOffice 转 PDF：单实例进程，必须串行（并发闸门 "office"）
OFFICE_SUFFIXES = (".doc", ".docx", ".xls", ".xlsx")

# 货物行右侧的计价列：Rate/Charge 与 Total 必须与计费重量同行、且在其右侧
CHARGE_COLUMN_KEYS: tuple[str, ...] = ("rate", "ratetotal")
# 同行判定：归一化坐标下的纵向容差（约 6pt / 842pt）
CHARGE_ROW_SLACK = 0.007

# 单证文本层应当出现的关键标签：只有数字、没有这些标签的文本层视为不可用
DOC_TEXT_LABELS: tuple[str, ...] = (
    "AIR WAYBILL",
    "WAYBILL",
    "HAWB",
    "MAWB",
    "SHIPPER",
    "CONSIGNEE",
    "MASTER",
    "HOUSE",
    "运单",
    "分单",
    "主单",
    "舱单",
    "MANIFEST",
)


def _drop_unreliable_charge_fields(
    fields: dict[str, str | None],
    locations: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """校验 Rate/Charge 与 Total 是否落在货物行对应列上，位置不对就置空。

    货物表格的这两列经常是空栏，模型会把下方费用区（Weight Charge、
    Total Prepaid 等）或品名栏（DIMS ... x 1）里的数字当成单价/总价。
    用定位结果兜底：不在计费重量右侧、或不同行，就视为空栏并清掉模型值。
    """

    anchor_boxes = locations.get("jfweight") or []
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


def _text_layer_usable(text: str, min_chars: int) -> bool:
    """文本层是否可用于分类。

    扫描件的文本层常常只有零散数字或表头碎片（字符数够但没有单证标签），
    这类页面继续用文本层会把分类带偏，应交给视觉模型逐字转写。
    """

    stripped = text.strip()
    if len(stripped) < min_chars:
        return False
    upper = stripped.upper()
    return any(label in upper for label in DOC_TEXT_LABELS)


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
        pages_path = self.store.pages_path(task_id)
        cached = self.store.read_json_or_none(pages_path)
        if cached and cached.get("pages"):
            return {
                "pages": cached["pages"],
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
                        source_file=item["name"],
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
        pages_path = self.store.pages_path(task_id)
        cached = self.store.read_json_or_none(pages_path) or {}
        if cached.get("transcribed"):
            return {
                "pages": cached.get("pages", []),
                "warnings": cached.get("warnings", []),
                "page_boundaries": cached.get("page_boundaries", []),
                "files": cached.get("files", []),
            }

        targets = [
            page
            for page in pages
            if page.image_rel
            and (
                self.settings.force_vlm
                or not _text_layer_usable(
                    page.text, self.settings.text_layer_min_chars
                )
            )
        ]
        warnings: list[str] = []
        if targets:
            await self._emit(
                state,
                "图像转写",
                f"{len(targets)} 页文本层不足，调用视觉模型逐字转写",
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
                if text and text.strip():
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
        await self._emit(
            state,
            "图像转写",
            f"文本准备完成，共 {len(pages)} 页",
            42,
            "warning" if empty_pages else "info",
        )
        return {
            "pages": [page.model_dump() for page in pages],
            "warnings": [*(state.get("warnings") or []), *warnings],
            "page_boundaries": cached.get("page_boundaries", []),
            "files": cached.get("files", []),
        }

    # ------------------------------------------------------------------ 3
    async def classify_pages(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        segments_path = self.store.segments_path(task_id)
        cached = self.store.read_json_or_none(segments_path)
        if cached:
            return {
                "segments": cached.get("segments", []),
                "master_bills": cached.get("master_bills", []),
                "warnings": cached.get("warnings", []),
                "pages": cached.get("pages", state.get("pages", [])),
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
            if master_bills:
                warnings.append(
                    "文件只包含主单，已跳过字段抽取；请补充分单（HAWB）与舱单后再提交"
                )
                hint = f"主单 {len(master_bills)} 份"
            elif manifest_pages:
                warnings.append(
                    "文件只包含舱单，已跳过字段抽取；请补充分单（HAWB）后再提交"
                )
                hint = f"舱单 {manifest_pages} 页"
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
                f"{hint}，未发现分单，已跳过字段抽取",
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
        house_path = self.store.house_bills_path(task_id)
        manifest_path = self.store.manifest_path(task_id)
        cached_house = self.store.read_json_or_none(house_path)
        cached_manifest = self.store.read_json_or_none(manifest_path)
        if cached_house is not None and cached_manifest is not None:
            return {
                "house_bills": cached_house.get("house_bills", []),
                "manifest": cached_manifest.get("manifest"),
                "warnings": [*state.get("warnings", []), *cached_house.get("warnings", [])],
            }

        pages = [PageRecord(**item) for item in state.get("pages") or []]
        segments = [Segment(**item) for item in state.get("segments") or []]
        warnings: list[str] = list(state.get("warnings") or [])

        house_segments = [item for item in segments if item.kind == "house_bill"]
        manifest_segments = [item for item in segments if item.kind == "manifest"]

        total = len(house_segments) + (1 if manifest_segments else 0)
        finished = 0
        semaphore = asyncio.Semaphore(self.settings.house_bill_concurrency)

        async def run_house(index: int, segment: Segment) -> HouseBill:
            nonlocal finished
            async with semaphore:
                bill_no = segment.doc_no or f"第 {index} 份分单"
                await self._emit(
                    state,
                    "抽取分单与舱单字段",
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
                    # 港口只接受三字码：确定不了就留空，原文留给待审核
                    field_evidence = normalize_port_fields(fields)
                    bill_warnings = result["warnings"]
                    confidence = result["confidence"]
                except (ModelCallFailedError, ExtractionError) as exc:
                    return HouseBill(
                        bill_id=f"bill_{index:03d}",
                        segment_id=segment.segment_id,
                        page_indexes=segment.page_indexes,
                        fields={},
                        confidence=0.0,
                        warnings=[f"分单抽取失败：{exc}"],
                    )
                finally:
                    finished += 1
                field_locations = self._locate_segment_fields(
                    task_id, pages, segment.page_indexes, fields, field_sources
                )
                # 计价列靠版面位置兜底：空栏时模型容易把别处的数字填进来
                bill_warnings = [
                    *bill_warnings,
                    *_drop_unreliable_charge_fields(fields, field_locations),
                ]
                return HouseBill(
                    bill_id=f"bill_{index:03d}",
                    segment_id=segment.segment_id,
                    page_indexes=segment.page_indexes,
                    fields=fields,
                    field_locations=field_locations,
                    field_evidence=field_evidence,
                    field_sources=field_sources,
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
                state, "抽取分单与舱单字段", "正在抽取舱单表格", 78
            )
            try:
                result = await extract_manifest(self.gateway, pages, page_indexes)
                entries = []
                for position, entry in enumerate(result["entries"], start=1):
                    entry_fields = entry["fields"]
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
                            warnings=entry["warnings"],
                        )
                    )
                master_payload = result.get("master")
                manifest_document = ManifestDocument(
                    segment_id=manifest_segments[0].segment_id,
                    page_indexes=page_indexes,
                    entries=entries,
                    master=(
                        ManifestMaster(**master_payload) if master_payload else None
                    ),
                    warnings=result["warnings"],
                )
                if not entries:
                    warnings.append("舱单未抽取出任何数据行")
            except (ModelCallFailedError, ExtractionError) as exc:
                warning = f"舱单抽取失败，无法核对：{exc}"
                warnings.append(warning)
                await self._emit(
                    state, "抽取分单与舱单字段", warning, 82, "error"
                )
        else:
            warnings.append("未识别到舱单，未执行字段核对")

        house_payload = [bill.model_dump() for bill in house_bills]
        manifest_payload = (
            manifest_document.model_dump() if manifest_document is not None else None
        )
        self.store.write_json_atomic(
            house_path, {"house_bills": house_payload, "warnings": warnings}
        )
        self.store.write_json_atomic(manifest_path, {"manifest": manifest_payload})
        await self._emit(
            state,
            "抽取分单与舱单字段",
            f"抽取完成：分单 {len(house_bills)} 份、舱单 "
            f"{len(manifest_document.entries) if manifest_document else 0} 行",
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

        manifest_payload = state.get("manifest")
        manifest = (
            ManifestDocument(**manifest_payload) if manifest_payload else None
        )
        house_bills = list(state.get("house_bills") or [])
        comparisons, summary, compare_warnings = compare(house_bills, manifest)
        totals = compare_totals(house_bills, manifest)
        warnings = [*(state.get("warnings") or []), *compare_warnings]
        totals_failed = [item for item in totals if item.status != "match"]

        payload = {
            "comparisons": [item.model_dump() for item in comparisons],
            "totals_checks": [item.model_dump() for item in totals],
            "summary": summary.model_dump(),
            "warnings": compare_warnings,
        }
        self.store.write_json_atomic(comparisons_path, payload)
        await self._emit(
            state,
            "核对分单与舱单",
            f"核对完成：一致 {summary.consistent} 份、不一致 {summary.inconsistent} 份、"
            f"信息不全 {summary.incomplete} 份、汇总差异 {len(totals_failed)} 项",
            92,
            "warning" if summary.inconsistent or totals_failed else "info",
        )
        return {
            "comparisons": payload["comparisons"],
            "totals_checks": payload["totals_checks"],
            "summary": payload["summary"],
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ 6
    async def build_result(self, state: AnalysisState) -> dict[str, Any]:
        task_id = state["task_id"]
        # 没有分单时图会跳过抽取与核对节点，这里补一条"已跳过"事件供前端展示
        if not any(
            item.get("kind") == "house_bill" for item in state.get("segments") or []
        ):
            for label in ("抽取分单与舱单字段", "核对分单与舱单"):
                await self._emit(state, label, "无分单，已跳过", 0, phase="skipped")
        pages = [PageRecord(**item) for item in state.get("pages") or []]
        house_bills = [HouseBill(**item) for item in state.get("house_bills") or []]
        manifest = ManifestDocument(**state["manifest"]) if state.get("manifest") else None
        comparisons = [
            HouseBillComparison(**item) for item in state.get("comparisons") or []
        ]
        totals_checks = [
            TotalsCheck(**item) for item in state.get("totals_checks") or []
        ]
        review_items = build_review_items(
            house_bills,
            comparisons,
            manifest,
            self.settings.review_confidence_threshold,
            totals_checks,
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
            manifest=manifest,
            comparisons=comparisons,
            totals_checks=totals_checks,
            summary=ComparisonSummary(**state.get("summary") or {}),
            review_items=review_items,
            warnings=_unique(state.get("warnings") or []),
        )
        self.store.write_json_atomic(
            self.store.result_path(task_id), result.model_dump(mode="json")
        )
        await self._emit(
            state,
            "生成结果",
            f"任务完成，共 {result.summary.total_house_bills} 份分单、"
            f"{result.summary.inconsistent} 份存在字段差异",
            100,
        )
        return {"result": result.model_dump(mode="json")}

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
        self.store.write_json_atomic(
            self.store.pages_path(task_id),
            {
                "parser": parser,
                "warnings": warnings,
                "transcribed": transcribed,
                "page_boundaries": page_boundaries or [],
                "files": files or [],
                "pages": [page.model_dump() for page in pages],
            },
        )

    def _save_segments(
        self,
        task_id: str,
        segments: list[Segment],
        master_bills: list[MasterBillInfo],
        warnings: list[str],
        pages: list[PageRecord],
    ) -> None:
        self.store.write_json_atomic(
            self.store.segments_path(task_id),
            {
                "segments": [item.model_dump() for item in segments],
                "master_bills": [item.model_dump() for item in master_bills],
                "warnings": warnings,
                "pages": [page.model_dump() for page in pages],
            },
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

    def _locate_segment_fields(
        self,
        task_id: str,
        pages: list[PageRecord],
        page_indexes: list[int],
        fields: dict[str, str | None],
        sources: dict[str, str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """在分单所在 PDF 中定位字段值，供前端点击字段时高亮。"""

        by_index = {page.index: page for page in pages}
        first = by_index.get(page_indexes[0]) if page_indexes else None
        if first is None:
            return {}
        pdf_path = self._resolve_source_pdf(task_id, first.source_file)
        if pdf_path is None:
            return {}
        page_map: dict[int, int] = {}
        for index in page_indexes:
            page = by_index.get(index)
            if page is None or page.source_file != first.source_file:
                continue
            page_map[index] = page.page_in_file or index
        # 定位优先用页面原文（港口归一成三字码后，原文才是页面上真实存在的内容）
        lookup = {
            key: (sources or {}).get(key) or value
            for key, value in fields.items()
        }
        return locate_fields(pdf_path, page_map, lookup)

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
