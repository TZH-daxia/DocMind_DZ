"""单证识别、抽取与核对的领域模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

DocKind = Literal["master_bill", "house_bill", "manifest", "other"]
TextSource = Literal["text_layer", "docx", "xlsx", "vlm", "empty"]
CompareStatus = Literal[
    "match",
    "similar",
    "mismatch",
    "missing_in_house_bill",
    "missing_in_manifest",
    "missing_both",
]
BillStatus = Literal["consistent", "inconsistent", "incomplete"]
TaskStatus = Literal[
    "queued", "running", "paused", "succeeded", "failed", "cancelled"
]
NodePhase = Literal["started", "succeeded", "failed", "skipped", ""]

DOC_KIND_LABELS: dict[str, str] = {
    "master_bill": "主单",
    "house_bill": "分单",
    "manifest": "舱单",
    "other": "其他",
}


class PageRecord(BaseModel):
    """渲染/解析后的一页：文本 + 可选页面图片。"""

    index: int = Field(description="1 起的页序号")
    text: str = ""
    # 转写全文的落盘路径（document.json 只记路径，正文在 transcription/page_00N.md）
    text_rel: str | None = None
    text_source: TextSource = "empty"
    image_rel: str | None = None
    char_count: int = 0
    kind: DocKind | None = None
    kind_confidence: float = 0.0
    kind_evidence: list[str] = Field(default_factory=list)
    # 多文件上传时记录来源文件与文件内页码
    source_file: str | None = None
    page_in_file: int | None = None
    # 分单页四象限局部精读结果（小字更清晰），抽取时优先于整页转写
    zoom_text: str | None = None
    zoom_rel: str | None = None


class Segment(BaseModel):
    """页面分段结果：一段连续页面 = 一份单据（主单/分单/舱单）。"""

    segment_id: str
    kind: DocKind
    label: str = ""
    page_indexes: list[int] = Field(default_factory=list)
    doc_no: str | None = None
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class MasterBillInfo(BaseModel):
    """主单识别结果：只登记、不抽字段。"""

    segment_id: str
    page_indexes: list[int] = Field(default_factory=list)
    master_no: str | None = None
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    skipped: bool = True


CandidateStatus = Literal["confirmed", "needs_review", "conflict", "missing"]


class FieldEvidence(BaseModel):
    """候选值的原文引用：quote 必须是 VLM 转写文档中的逐字片段。"""

    quote: str | None = None
    page: int | None = None


class FieldCandidate(BaseModel):
    """一个字段的候选：值 + 状态 + 置信度 + 原文引用，用于逐字段复核。"""

    field_key: str
    value: str | None = None
    raw_value: str | None = None
    status: CandidateStatus = "confirmed"
    confidence: float = 0.6
    evidence: list[FieldEvidence] = Field(default_factory=list)
    # 同一字段在页面出现多个不同值时的其它候选（不擅自替用户选择）
    alternatives: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class HouseBill(BaseModel):
    bill_id: str
    segment_id: str
    page_indexes: list[int] = Field(default_factory=list)
    fields: dict[str, str | None] = Field(default_factory=dict)
    # 字段在原图中的位置：key → [{page, bbox:[x,y,w,h]}]，供前端点击字段时高亮
    field_locations: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # 定位用的页面原文（如港口归一成三字码后，原文仍用于在原图上找位置）
    field_sources: dict[str, str] = Field(default_factory=dict)
    # 字段级候选（含 missing），便于对照「转写 vs 提取」与逐字段复核
    candidates: list[FieldCandidate] = Field(default_factory=list)
    confidence: float = 0.6
    warnings: list[str] = Field(default_factory=list)
    source_excerpt: str = ""


class ManifestEntry(BaseModel):
    entry_id: str
    line_no: int | None = None
    page_indexes: list[int] = Field(default_factory=list)
    fields: dict[str, str | None] = Field(default_factory=dict)
    # 字段在原图中的位置：key → [{page, bbox}]，点击舱单字段时高亮
    field_locations: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    confidence: float = 0.6
    warnings: list[str] = Field(default_factory=list)


class ManifestMaster(BaseModel):
    """舱单里的总运单（主单）汇总行：用于和分单合计值核对。"""

    mawb_no: str | None = None
    dest_port: str | None = None
    pieces: str | None = None
    weight: str | None = None
    chargeable_weight: str | None = None
    volume: str | None = None


class ManifestDocument(BaseModel):
    segment_id: str
    page_indexes: list[int] = Field(default_factory=list)
    entries: list[ManifestEntry] = Field(default_factory=list)
    master: ManifestMaster | None = None
    warnings: list[str] = Field(default_factory=list)


class FieldComparison(BaseModel):
    key: str
    label: str
    house_value: str | None = None
    manifest_value: str | None = None
    status: CompareStatus
    similarity: float = 1.0
    note: str = ""


class HouseBillComparison(BaseModel):
    bill_id: str
    hawb_no: str | None = None
    manifest_entry_id: str | None = None
    manifest_line_no: int | None = None
    status: BillStatus = "incomplete"
    match_count: int = 0
    mismatch_count: int = 0
    missing_count: int = 0
    fields: list[FieldComparison] = Field(default_factory=list)


class ComparisonSummary(BaseModel):
    total_house_bills: int = 0
    total_manifest_entries: int = 0
    consistent: int = 0
    inconsistent: int = 0
    incomplete: int = 0
    house_only: int = 0
    manifest_only: int = 0
    field_mismatch_count: int = 0


class TotalsCheck(BaseModel):
    """汇总核对：分单合计值 vs 舱单总运单汇总值。"""

    key: str
    label: str
    status: Literal["match", "mismatch", "missing"] = "missing"
    house_total: str | None = None
    manifest_total: str | None = None
    note: str = ""


class ReviewItem(BaseModel):
    """待人工审核的一条记录（差异 / 缺失 / 近似 / 低置信度）。"""

    tab_id: str
    bill_id: str | None = None
    hawb_no: str | None = None
    key: str
    label: str
    reason: Literal["mismatch", "missing", "similar", "low_confidence"]
    message: str
    # 原文引用：前端在字段下展示「原文：…」，用于人工确认识别是否准确
    evidence: str = ""


class AnalysisResult(BaseModel):
    task_id: str
    source_name: str
    status: TaskStatus = "succeeded"
    created_at: datetime
    finished_at: datetime | None = None
    elapsed_seconds: float = 0.0
    page_count: int = 0
    parser: str = ""
    pages: list[PageRecord] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    master_bills: list[MasterBillInfo] = Field(default_factory=list)
    house_bills: list[HouseBill] = Field(default_factory=list)
    manifest: ManifestDocument | None = None
    comparisons: list[HouseBillComparison] = Field(default_factory=list)
    totals_checks: list[TotalsCheck] = Field(default_factory=list)
    summary: ComparisonSummary = Field(default_factory=ComparisonSummary)
    review_items: list[ReviewItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TaskError(BaseModel):
    code: str
    message: str


class TaskSnapshot(BaseModel):
    task_id: str
    source_name: str
    status: TaskStatus = "queued"
    progress: int = 0
    step: str = "已排队"
    error: TaskError | None = None
    created_at: datetime
    finished_at: datetime | None = None
    house_bill_count: int = 0
    manifest_entry_count: int = 0
    inconsistent_count: int = 0
    # 结果文件是否已落盘：弹窗页签只显示有结果的任务
    result_available: bool = False
    submitted: bool = False
    submitted_at: datetime | None = None
    submission_code: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class WorkflowEvent(BaseModel):
    seq: int = 0
    at: datetime
    level: Literal["info", "warning", "error"] = "info"
    step: str = ""
    message: str = ""
    progress: int = 0
    # 节点阶段与耗时：started 只表示开始，succeeded/failed 带 duration_ms
    phase: NodePhase = ""
    duration_ms: int | None = None
