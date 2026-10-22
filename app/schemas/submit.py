"""提交请求/响应模型。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class SubmitDocument(BaseModel):
    """弹窗中的一个页签：一份分单，或整份舱单。"""

    tab_id: str = Field(description="页签 ID：分单为 bill_id，舱单为 manifest")
    kind: Literal["house_bill", "manifest"] = Field(description="单据类型")
    fields: dict[str, str | None] = Field(
        default_factory=dict, description="字段 key → 人工核对后的值"
    )
    entries: list[dict[str, Any]] = Field(
        default_factory=list, description="舱单行（仅舱单页签使用）"
    )


class SubmitRequest(BaseModel):
    documents: list[SubmitDocument] = Field(
        default_factory=list, description="待提交的页签"
    )
    acknowledged: bool = Field(
        default=False, description="是否已确认差异；存在未确认差异时提交会被拒绝"
    )
    request_id: str | None = Field(
        default=None, description="幂等请求 ID，重复提交同一 ID 返回首次结果"
    )


class SubmitResponse(BaseModel):
    ok: bool = True
    submission_code: str
    submitted_at: str
    duplicated: bool = False
