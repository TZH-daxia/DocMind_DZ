"""主单识别与页面分段。

规则优先级（顺序不能颠倒）：
1. 舱单特征（舱单/MANIFEST/CARGO MANIFEST）→ manifest
2. 分单特征（HAWB/HOUSE AIR WAYBILL/分单号）→ house_bill
3. 主单特征（MAWB/MASTER AIR WAYBILL/主单号/总单号）→ master_bill
4. 仅出现 11 位主单号格式 + 至少一个辅助特征 → master_bill（低置信度）

之所以把分单排在主单前面：分单页面经常以 "MAWB No. 999-12345678" 引用主单号，
若先判主单，会把整份分单误判成主单并跳过。只有同时具备主单标题类特征、或
"只有主单号没有任何分单标识" 的页面才归为主单。

无法由规则确定的页面交给 LLM 分类（见 `app.service.pipeline`），本模块只负责
确定性规则与分段合并。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.schemas.documents import DOC_KIND_LABELS, DocKind

MASTER_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bMAWB\b",
        # 单证标题：Air Waybill（主单常见；"HOUSE AIR WAYBILL" 会在 detect_master_bill 里先行排除）
        r"\bAIR\s+WAYBILL\b",
        r"MASTER\s+AIR\s+WAYBILL",
        r"MASTER\s+AWB\b",
        r"MASTER\s+WAYBILL",
        r"主单",
        r"总单",
        r"主运单",
        r"总运单",
    )
)

HOUSE_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bHAWB\b",
        r"HOUSE\s+AIR\s+WAYBILL",
        r"HOUSE\s+AWB\b",
        r"HOUSE\s+WAYBILL",
        r"分单",
        r"分运单",
    )
)

MANIFEST_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"舱单",
        r"\bMANIFEST\b",
        r"CARGO\s+MANIFEST",
        r"装载清单",
        r"载货清单",
    )
)

# 主单号：3 位航司前缀 + 8 位序号。
# 真实单证里分隔符可能是 "- "、" - " 或没有（如 416- 52331591、41652331591）
MASTER_NO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\d)(\d{3})[-\s]{0,2}(\d{8})(?!\d)"),
    re.compile(r"(?<!\d)(\d{3})[-\s]{0,2}(\d{4})[-\s]{0,2}(\d{4})(?!\d)"),
)

MAWB_REFERENCE_PATTERN = re.compile(
    r"(?:MAWB|MASTER\s+(?:AIR\s+)?WAYBILL|主单号|总单号)\s*(?:NO\.?|NUMBER|#)?\s*[:：]?\s*"
    r"([A-Z]{0,3}[\s-]?\d{3}[\s-]?\d{4,8})",
    re.IGNORECASE,
)

HAWB_NO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:HAWB|HOUSE\s+(?:AIR\s+)?WAYBILL)\s*(?:NO\.?|NUMBER|#|号)?\s*[:：]?\s*"
        r"([A-Z0-9][A-Z0-9\-]{4,24})",
        re.IGNORECASE,
    ),
    re.compile(r"(?:分单号|分运单号)\s*[:：]?\s*([A-Z0-9][A-Z0-9\-]{4,24})", re.IGNORECASE),
)

FLIGHT_NO_PATTERN = re.compile(
    r"(?:航班号|FLIGHT\s*(?:NO\.?|NUMBER)?|FLT\.?)\s*[:：]?\s*([A-Z0-9]{2,3}\s?-?\s?\d{1,4}[A-Z]?)",
    re.IGNORECASE,
)
FLIGHT_NO_LOOSE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{3,4})(?![A-Z0-9])")

FLIGHT_DATE_PATTERN = re.compile(
    r"(?:航班日期|航班日|FLIGHT\s*DATE|DATE\s*OF\s*(?:DEPARTURE|FLIGHT)|DEPARTURE\s*DATE)"
    r"\s*(?:[:：]|\s)\s*"
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{1,2}\s*[A-Z]{3}\s*\d{2,4}"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日?)",
    re.IGNORECASE,
)

PARTY_HINT_PATTERN = re.compile(
    r"SHIPPER|CONSIGNEE|发货人|收货人|托运人|收货单位", re.IGNORECASE
)
FLIGHT_HINT_PATTERN = re.compile(
    r"始发港|目的港|ORIGIN|DESTINATION|FLIGHT|航班", re.IGNORECASE
)
CONTINUATION_HINT_PATTERN = re.compile(
    r"续页|接上页|CONTD?\.?|CONTINUED|PAGE\s*\d+\s*(?:OF|/)\s*\d+", re.IGNORECASE
)

NOISE_DOC_NOS = {
    "NUMBER",
    "NO",
    "MAWB",
    "HAWB",
    "AIRWAYBILL",
    "WAYBILL",
    "MASTER",
    "HOUSE",
    "TOTAL",
}


def normalize_for_match(text: str) -> str:
    """全角转半角、压缩空白，便于正则匹配。"""

    normalized = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[ \t\u3000]+", " ", normalized)


def normalize_doc_no(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", unicodedata.normalize("NFKC", value)).upper()
    if not cleaned or cleaned in NOISE_DOC_NOS or len(cleaned) < 5:
        return None
    return cleaned


def find_hawb_no(text: str) -> str | None:
    normalized = normalize_for_match(text)
    for pattern in HAWB_NO_PATTERNS:
        # 标题行可能先被匹配到（如 "HOUSE AIR WAYBILL"），必须继续找下一个有效值
        for match in pattern.finditer(normalized):
            value = normalize_doc_no(match.group(1))
            if value:
                return value
    return None


PHONE_CONTEXT_PATTERN = re.compile(
    r"(?:TEL|TEL[:：.]|PH|PH[:：.]|FAX|MOBILE|CELL|CONTACT|电话|手机|联系方式)",
    re.IGNORECASE,
)


def find_master_no(text: str) -> str | None:
    """找出主单号（3 位航司代码 + 8 位序号）。

    页面里的电话号码同样是 11 位数字（如 TEL:13162113320），必须按上下文排除；
    多个候选时优先「重复出现的」「带连字符的」「靠近 MAWB 标签的」。
    """

    normalized = normalize_for_match(text)
    compact = re.sub(r"[^0-9A-Z]", "", normalized.upper())
    candidates: list[tuple[str, int, str]] = []
    for pattern in MASTER_NO_PATTERNS:
        for match in pattern.finditer(normalized):
            digits = "".join(group for group in match.groups() if group)
            if len(digits) != 11:
                continue
            prefix = normalized[max(0, match.start() - 18) : match.start()]
            if PHONE_CONTEXT_PATTERN.search(prefix):
                continue
            candidates.append((digits, match.start(), match.group(0)))
    if not candidates:
        return None

    best_digits: str | None = None
    best_score = -1
    for digits, start, raw in candidates:
        score = compact.count(digits) * 2
        if "-" in raw or " " in raw:
            score += 1
        label_window = normalized[max(0, start - 40) : start]
        if re.search(r"MAWB|MASTER|主单|总单", label_window, re.IGNORECASE):
            score += 5
        if score > best_score:
            best_score = score
            best_digits = digits
    return f"{best_digits[:3]}-{best_digits[3:]}" if best_digits else None


def find_flight_no(text: str) -> str | None:
    normalized = normalize_for_match(text)
    match = FLIGHT_NO_PATTERN.search(normalized)
    if match:
        return re.sub(r"[\s-]", "", match.group(1)).upper()
    match = FLIGHT_NO_LOOSE.search(normalized)
    return match.group(1).upper() if match else None


def find_flight_date(text: str) -> str | None:
    normalized = normalize_for_match(text)
    match = FLIGHT_DATE_PATTERN.search(normalized)
    return match.group(1).strip() if match else None


def _hit_labels(patterns: tuple[re.Pattern[str], ...], text: str) -> list[str]:
    hits: list[str] = []
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            hits.append(match.group(0).strip())
    return hits


def _manifest_hits(text: str) -> list[str]:
    """过滤掉 "CONSOLIDATION AS PER ATTACHED MANIFEST" 这类品名说明。"""

    hits: list[str] = []
    for pattern in MANIFEST_LABEL_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 48) : match.end() + 48]
            if re.search(r"AS\s+PER|ATTACHED|PER\s+ATTACHED", window, re.IGNORECASE):
                continue
            hits.append(match.group(0).strip())
            break
    return hits


@dataclass
class PageKind:
    """单页判定结果（规则或 LLM）。"""

    index: int
    kind: DocKind | None = None
    doc_no: str | None = None
    continuation: bool = False
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    source: str = "rules"


@dataclass
class MasterBillSignal:
    is_master: bool
    confidence: float
    master_no: str | None
    evidence: list[str]


def detect_master_bill(text: str) -> MasterBillSignal:
    """判断页面是否为主单，并给出确认依据（供前端展示"为什么跳过"）。

    只有出现「具体分单号」或 HOUSE AIR WAYBILL 标题时才排除主单；仅仅出现
    "CONSOLIDATION AS PER ATTACHED MANIFEST" 之类字样并不影响主单判定
    （主单的品名栏经常这么写）。
    """

    normalized = normalize_for_match(text)
    hawb_no = find_hawb_no(normalized)
    has_house_title = bool(
        re.search(r"HOUSE\s+AIR\s+WAYBILL|HOUSE\s+AWB\b", normalized, re.IGNORECASE)
    )
    if hawb_no or has_house_title:
        return MasterBillSignal(False, 0.0, None, [])

    master_hits = _hit_labels(MASTER_LABEL_PATTERNS, normalized)
    master_no = find_master_no(normalized)

    evidence: list[str] = []
    if master_hits:
        evidence.append("命中主单标题特征：" + "、".join(master_hits))
    if master_no:
        evidence.append(f"识别到主单号格式（3 位航司代码 + 8 位序号）：{master_no}")

    if master_hits and master_no:
        return MasterBillSignal(True, 0.96, master_no, evidence)

    auxiliary = 0
    if FLIGHT_HINT_PATTERN.search(normalized):
        auxiliary += 1
        evidence.append("包含航班/起讫港字段")
    if PARTY_HINT_PATTERN.search(normalized):
        auxiliary += 1
        evidence.append("包含发货人/收货人字段")
    if re.search(r"航空货运单|AIR\s+WAYBILL", normalized, re.IGNORECASE):
        auxiliary += 1
        evidence.append("包含航空货运单标题")

    occurrences = 0
    if master_no:
        compact = re.sub(r"[^0-9A-Z]", "", normalized.upper())
        occurrences = compact.count(master_no.replace("-", ""))
        if occurrences >= 2:
            evidence.append(f"主单号在页面出现 {occurrences} 次")

    if master_hits and auxiliary >= 1:
        return MasterBillSignal(True, 0.88, master_no, evidence)
    # 主单号重复出现 + 任一单证特征：即使没有 "MAWB" 字样也按主单处理
    if master_no and occurrences >= 2 and auxiliary >= 1:
        return MasterBillSignal(True, 0.85, master_no, evidence)
    # 承运人运单常见版式：主单号在页首/页尾反复出现，但没有分单标识
    if master_no and occurrences >= 3:
        return MasterBillSignal(True, 0.72, master_no, evidence)
    return MasterBillSignal(False, 0.0, None, [])


def _head_lines(text: str, count: int = 8) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:count])


def _has_manifest_title(head: str) -> bool:
    """标题行判定：排除 "CONSOLIDATION AS PER ATTACHED MANIFEST" 这类品名说明。"""

    for line in head.splitlines():
        stripped = line.strip()
        if not stripped or re.search(r"AS\s+PER|ATTACHED|PER\s+ATTACHED", stripped, re.I):
            continue
        # 允许行首出现表格分隔符（Excel 转出的文本常见 "| | CARGO MANIFEST"）
        if re.match(
            r"^\W*(?:(?:CARGO|AIR|FREIGHT|CONSOL)\s+)*MANIFEST\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        if re.match(r"^(?:\S{0,8})?舱单", stripped) or stripped in {"舱单", "装载清单", "载货清单"}:
            return True
    return False


def _score_manifest(text: str, head: str) -> int:
    """舱单强度：标题必须在页首附近，其次才看表格特征。"""

    score = 0
    if _has_manifest_title(head):
        score += 3
    if re.search(
        r"SHIPMENT\s+DETAILS|CONSOL\s+DETAILS|MASTER\s+FREIGHT|CONSOL\s+DETAILS",
        text,
        re.IGNORECASE,
    ):
        score += 2
    if re.search(r"\bHOUSE\b", text, re.IGNORECASE) and re.search(
        r"HAWB\s*[:：]", text, re.IGNORECASE
    ):
        score += 2
    if re.search(r"TOTAL\s+WEIGHT|TOTAL\s+VOLUME", text, re.IGNORECASE):
        score += 1
    if re.search(r"PAGE\s*(?:NO\.?)?\s*\d+\s*(?:OF|/)\s*\d+", text, re.IGNORECASE):
        score += 1
    if re.search(r"\bMANIFEST\b|舱单", text, re.IGNORECASE):
        score += 1
    return score


def _score_house(text: str, head: str, hawb_no: str | None) -> int:
    """分单强度：关键是有没有"具体分单号"，而不是共享的 IATA 表格标签。"""

    score = 0
    if hawb_no:
        score += 4
    if re.search(r"HOUSE\s+AIR\s+WAYBILL|HOUSE\s+AWB\b", text, re.IGNORECASE):
        score += 3
    if re.search(r"HAWB\s*(?:NO\.?|NUMBER|#)\s*[:：]?", head, re.IGNORECASE):
        score += 2
    if re.search(r"Shipper'?s\s+Name\s+and\s+Address", text, re.IGNORECASE):
        score += 1
    if re.search(r"Issuing\s+Carrier'?s\s+Agent", text, re.IGNORECASE):
        score += 1
    return score


def classify_page_by_rules(text: str, previous_kind: DocKind | None) -> PageKind:
    """单页规则判定；`kind is None` 表示需交给 LLM。"""

    normalized = normalize_for_match(text)
    if len(normalized.strip()) < 10:
        return PageKind(index=0, kind=None, confidence=0.0, evidence=["页面文本为空"])

    head = _head_lines(normalized)
    manifest_hits = _manifest_hits(normalized)
    house_hits = _hit_labels(HOUSE_LABEL_PATTERNS, normalized)
    hawb_no = find_hawb_no(normalized)
    master_signal = detect_master_bill(normalized)
    manifest_score = _score_manifest(normalized, head)
    house_score = _score_house(normalized, head, hawb_no)

    if manifest_score >= 4 and manifest_score >= house_score:
        return PageKind(
            index=0,
            kind="manifest",
            doc_no=None,
            confidence=0.95,
            evidence=[
                "舱单特征得分 {}：{}".format(
                    manifest_score, "、".join(manifest_hits) or "表格结构"
                )
            ],
        )
    if house_score >= 3:
        return PageKind(
            index=0,
            kind="house_bill",
            doc_no=hawb_no,
            confidence=0.93,
            evidence=[
                "分单特征得分 {}：{}".format(
                    house_score, "、".join(house_hits) or "分单号/收发货人栏"
                )
            ],
        )
    if master_signal.is_master:
        return PageKind(
            index=0,
            kind="master_bill",
            doc_no=master_signal.master_no,
            confidence=master_signal.confidence,
            evidence=master_signal.evidence,
        )
    if manifest_hits:
        return PageKind(
            index=0,
            kind="manifest",
            doc_no=None,
            confidence=0.8,
            evidence=["命中舱单特征：" + "、".join(manifest_hits)],
        )
    if hawb_no or house_hits:
        return PageKind(
            index=0,
            kind="house_bill",
            doc_no=hawb_no,
            confidence=0.8,
            evidence=["命中分单特征：" + "、".join(house_hits)],
        )

    # 无标题：依据上一页判断是否为续页
    if previous_kind in {"master_bill", "house_bill", "manifest"}:
        if CONTINUATION_HINT_PATTERN.search(normalized):
            return PageKind(
                index=0,
                kind=previous_kind,
                continuation=True,
                confidence=0.7,
                evidence=["页面标注续页"],
            )
        if previous_kind == "manifest" and _looks_tabular(normalized):
            return PageKind(
                index=0,
                kind="manifest",
                continuation=True,
                confidence=0.6,
                evidence=["承接上一页舱单表格"],
            )
        if previous_kind == "house_bill" and (
            PARTY_HINT_PATTERN.search(normalized) or FLIGHT_HINT_PATTERN.search(normalized)
        ):
            return PageKind(
                index=0,
                kind="house_bill",
                continuation=True,
                confidence=0.55,
                evidence=["承接上一页分单字段"],
            )
    return PageKind(index=0, kind=None, confidence=0.0)


def _looks_tabular(text: str) -> bool:
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return False
    multi_column = sum(1 for line in lines if line.count("|") >= 2 or "  " in line)
    has_digits = sum(1 for line in lines if re.search(r"\d", line))
    return multi_column >= max(2, len(lines) // 2) and has_digits >= max(2, len(lines) // 3)


def classify_pages(
    pages: list[tuple[int, str]], boundaries: set[int] | None = None
) -> list[PageKind]:
    """对 (页号, 文本) 列表做规则判定，保留上一页类型处理续页。

    boundaries 是每个文件第一页的页号：跨文件不沿用「续页」语义。
    """

    boundaries = boundaries or set()
    results: list[PageKind] = []
    previous_kind: DocKind | None = None
    for index, text in pages:
        if index in boundaries:
            previous_kind = None
        kind = classify_page_by_rules(text, previous_kind)
        kind.index = index
        results.append(kind)
        if kind.kind in {"master_bill", "house_bill", "manifest"}:
            previous_kind = kind.kind
    return results


@dataclass
class SegmentDraft:
    kind: DocKind
    page_indexes: list[int]
    doc_no: str | None = None
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return DOC_KIND_LABELS.get(self.kind, self.kind)


def group_segments(
    page_kinds: list[PageKind], boundaries: set[int] | None = None
) -> list[SegmentDraft]:
    """把逐页判定合并成单据分段。

    - 舱单：一个文件只应有一份舱单，所有舱单页合并（跨页续表）；
    - 主单：连续主单页合并为一份，被其他单据/文件边界打断则另起一份；
    - 分单：非续页、跨文件或分单号变化即视为新分单。
    """

    boundaries = boundaries or set()
    segments: list[SegmentDraft] = []
    current_house: SegmentDraft | None = None
    current_master: SegmentDraft | None = None
    manifest: SegmentDraft | None = None

    for page in sorted(page_kinds, key=lambda item: item.index):
        if page.kind == "manifest":
            if manifest is None:
                manifest = SegmentDraft(kind="manifest", page_indexes=[])
                segments.append(manifest)
            manifest.page_indexes.append(page.index)
            manifest.confidence = max(manifest.confidence, page.confidence)
            manifest.evidence.extend(
                item for item in page.evidence if item not in manifest.evidence
            )
            continue

        if page.kind == "master_bill":
            current_house = None
            if current_master is None or page.index in boundaries:
                current_master = SegmentDraft(kind="master_bill", page_indexes=[])
                segments.append(current_master)
            current_master.page_indexes.append(page.index)
            current_master.confidence = max(current_master.confidence, page.confidence)
            current_master.doc_no = current_master.doc_no or page.doc_no
            current_master.evidence.extend(
                item for item in page.evidence if item not in current_master.evidence
            )
            continue

        current_master = None
        if page.kind == "house_bill":
            starts_new = (
                current_house is None
                or page.index in boundaries
                or not page.continuation
                or (
                    page.doc_no is not None
                    and current_house.doc_no is not None
                    and page.doc_no != current_house.doc_no
                )
            )
            if starts_new:
                current_house = SegmentDraft(
                    kind="house_bill", page_indexes=[], doc_no=page.doc_no
                )
                segments.append(current_house)
            current_house.page_indexes.append(page.index)
            current_house.doc_no = current_house.doc_no or page.doc_no
            current_house.confidence = max(current_house.confidence, page.confidence)
            current_house.evidence.extend(
                item for item in page.evidence if item not in current_house.evidence
            )
            continue

        current_house = None

    return segments
