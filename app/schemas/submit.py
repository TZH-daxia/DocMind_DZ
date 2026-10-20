"""提交请求/响应模型。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class SubmitDocument(BaseModel):
    """弹窗中的一个页签：一份分单，或整份舱单。"""

    tab_id: str
    kind: Literal["house_bill", "manifest"]
    fields: dict[str, str | None] = Field(default_factory=dict)
    entries: list[dict[str, Any]] = Field(default_factory=list)


class SubmitRequest(BaseModel):
    documents: list[SubmitDocument] = Field(default_factory=list)
    acknowledged: bool = False
    request_id: str | None = None


class SubmitResponse(BaseModel):
    ok: bool = True
    submission_code: str
    submitted_at: str
    duplicated: bool = False
