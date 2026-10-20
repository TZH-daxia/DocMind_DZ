"""LangGraph 状态。

`reporter` 是进程内的进度回调，不落盘、不跨进程；其余字段都是可 JSON 序列化的
中间结果，便于断点续跑（产物存在即节点已完成）。
"""

from typing import Any, TypedDict


class AnalysisState(TypedDict, total=False):
    task_id: str
    uploaded_path: str
    files: list[dict[str, Any]]
    source_name: str
    context: dict[str, Any]
    reporter: Any
    completed_nodes: list[str]
    created_at: str
    parser: str
    warnings: list[str]
    pages: list[dict[str, Any]]
    page_boundaries: list[int]
    segments: list[dict[str, Any]]
    master_bills: list[dict[str, Any]]
    house_bills: list[dict[str, Any]]
    manifest: dict[str, Any] | None
    comparisons: list[dict[str, Any]]
    summary: dict[str, Any]
    result: dict[str, Any]
