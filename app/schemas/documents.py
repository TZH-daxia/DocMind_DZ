"""单证识别、抽取与核对的领域模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.fields import FIELD_KEYS

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
    text_source: TextSource = "empty"
    image_rel: str | None = None
    char_count: int = 0
    kind: DocKind | None = None
    kind_confidence: float = 0.0
    kind_evidence: list[str] = Field(default_factory=list)
    # 多文件上传时记录来源文件与文件内页码
    source_file: str | None = None
    page_in_file: int | None = None

    @property
    def needs_vlm(self) -> bool:
        return self.image_rel is not None


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


class DocumentFields(BaseModel):
    """分单/舱单共有的 16 字段扁平结构（核对口径）。"""

    values: dict[str, str | None] = Field(default_factory=lambda: dict.fromkeys(FIELD_KEYS))


class HouseBill(BaseModel):
    bill_id: str
    segment_id: str
    page_indexes: list[int] = Field(default_factory=list)
    fields: dict[str, str | None] = Field(default_factory=dict)
    confidence: float = 0.6
    warnings: list[str] = Field(default_factory=list)
    source_excerpt: str = ""


class ManifestEntry(BaseModel):
    entry_id: str
    line_no: int | None = None
    page_indexes: list[int] = Field(default_factory=list)
    fields: dict[str, str | None] = Field(default_factory=dict)
    confidence: float = 0.6
    warnings: list[str] = Field(default_factory=list)


class ManifestDocument(BaseModel):
    segment_id: str
    page_indexes: list[int] = Field(default_factory=list)
    entries: list[ManifestEntry] = Field(default_factory=list)
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

    @property
    def key_mismatch(self) -> bool:
        return self.manifest_entry_id is None


class ComparisonSummary(BaseModel):
    total_house_bills: int = 0
    total_manifest_entries: int = 0
    consistent: int = 0
    inconsistent: int = 0
    incomplete: int = 0
    house_only: int = 0
    manifest_only: int = 0
    field_mismatch_count: int = 0


class ReviewItem(BaseModel):
    """待人工审核的一条记录（差异 / 缺失 / 近似 / 低置信度）。"""

    tab_id: str
    bill_id: str | None = None
    hawb_no: str | None = None
    key: str
    label: str
    reason: Literal["mismatch", "missing", "similar", "low_confidence"]
    message: str


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
