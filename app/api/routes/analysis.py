"""任务相关接口：上传、进度（SSE）、结果、页面图片、重试与删除。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.api.dependencies import get_task_service
from app.schemas.fields import HOUSE_FIELD_SPECS, MANIFEST_FIELD_SPECS
from app.schemas.file import UploadedDocument
from app.schemas.submit import SubmitRequest
from app.service.task_service import TaskError, TaskService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["分析任务"])


def _http_error(exc: TaskError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/ports", summary="港口三字码搜索")
async def search_ports(
    keyword: str = "",
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """前端港口输入框的搜索下拉：只查本地 hbinfo.json，不调模型。"""

    return service.search_ports(keyword)


@router.post("/tasks", summary="上传单证并创建分析任务")
async def create_task(
    file: UploadFile = File(..., description="单证文件（PDF / Word / Excel）"),
    context: str | None = Form(
        default=None, description="附加上下文，JSON 字符串（可选）"
    ),
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """一个文件一个任务：多个文件请分多次上传，各自独立分析。"""

    parsed_context: dict[str, Any] = {}
    if context:
        try:
            value = json.loads(context)
            parsed_context = value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400,
                detail={"code": "CONTEXT_INVALID", "message": "context 必须是合法 JSON"},
            ) from None

    upload = UploadedDocument(
        filename=file.filename or "document",
        content=await file.read(),
        content_type=file.content_type,
    )
    try:
        snapshot = service.create_and_start(upload, parsed_context)
    except TaskError as exc:
        raise _http_error(exc) from exc
    return snapshot.model_dump(mode="json")


@router.get("/tasks", summary="任务列表")
async def list_tasks(service: TaskService = Depends(get_task_service)) -> list[dict[str, Any]]:
    """返回所有任务的状态快照：状态、进度、分单/舱单条数与差异计数。"""

    return [task.model_dump(mode="json") for task in service.list_tasks()]


@router.get("/tasks/{task_id}", summary="任务状态与进度")
async def get_task(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """返回单个任务的状态快照，前端轮询刷新进度用。"""

    try:
        return service.get_snapshot(task_id).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/result", summary="任务结果")
async def get_result(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """返回分析结果：分单字段、舱单条目、核对差异与待审核项。"""

    try:
        return service.get_result(task_id)
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/events/history", summary="历史进度事件")
async def get_event_history(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> list[dict[str, Any]]:
    """返回任务已产生的全部进度事件，刷新页面后回填用。"""

    try:
        service.get_snapshot(task_id)
    except TaskError as exc:
        raise _http_error(exc) from exc
    return [event.model_dump(mode="json") for event in service.read_events(task_id)]


@router.get("/tasks/{task_id}/events", summary="实时进度（SSE）")
async def stream_events(
    task_id: str,
    request: Request,
    service: TaskService = Depends(get_task_service),
) -> StreamingResponse:
    """SSE 持续推送进度事件；任务进入终态时发送 done 事件后关闭。"""

    try:
        service.get_snapshot(task_id)
    except TaskError as exc:
        raise _http_error(exc) from exc

    async def event_stream():
        last_seq = 0
        while True:
            if await request.is_disconnected():
                break
            events = service.read_events(task_id, last_seq)
            for event in events:
                last_seq = event.seq
                yield f"data: {event.model_dump_json()}\n\n"
            try:
                snapshot = service.get_snapshot(task_id)
            except TaskError:
                break
            if snapshot.status in {"succeeded", "failed", "cancelled"} and not events:
                yield f"event: done\ndata: {snapshot.model_dump_json()}\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}/pages/{index}", summary="页面图片")
async def get_page_image(
    task_id: str,
    index: int,
    service: TaskService = Depends(get_task_service),
) -> FileResponse:
    """返回页面的 PNG 图片，用于原单预览与字段高亮定位。"""

    try:
        path = service.page_image(task_id, index)
    except TaskError as exc:
        raise _http_error(exc) from exc
    return FileResponse(path, media_type="image/png")


@router.get("/tasks/{task_id}/submission", summary="已提交结果")
async def get_submission(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """返回已提交的结果与提交单号；未提交时返回空。"""

    try:
        return {"submission": service.get_submission(task_id)}
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/submit", summary="提交核对结果")
async def submit_task(
    task_id: str,
    payload: SubmitRequest,
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """提交人工核对后的字段；仍存在未确认差异时会被拒绝。"""

    try:
        return service.submit(task_id, payload).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/pause", summary="暂停任务")
async def pause_task(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """暂停在途任务：已完成的节点产物保留，可继续。"""

    try:
        return service.pause_task(task_id).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/resume", summary="继续任务")
async def resume_task(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """从暂停处继续：只执行没有落盘产物的节点。"""

    try:
        return service.resume_task(task_id).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
async def cancel_task(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """取消任务：不可恢复，停在当前进度。"""

    try:
        return service.cancel_task(task_id).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


def _spec_catalog(specs) -> list[dict[str, Any]]:
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "group": spec.group,
            "kind": spec.kind,
        }
        for spec in specs
    ]


@router.get("/fields", summary="字段目录")
async def field_catalog() -> dict[str, list[dict[str, Any]]]:
    """分单与舱单共用同一套 AWB 模板字段。"""

    return {
        "house": _spec_catalog(HOUSE_FIELD_SPECS),
        "manifest": _spec_catalog(MANIFEST_FIELD_SPECS),
    }
