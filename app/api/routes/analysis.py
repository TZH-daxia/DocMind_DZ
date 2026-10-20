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

router = APIRouter(prefix="/analysis", tags=["analysis"])


def _http_error(exc: TaskError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.post("/tasks")
async def create_task(
    files: list[UploadFile] | None = File(default=None),
    file: UploadFile | None = File(default=None),
    context: str | None = Form(default=None),
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """一次可上传多个文件（如分单 PDF + 舱单 XLSX），合并为一个分析任务。"""

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

    incoming = [*(files or []), *([file] if file is not None else [])]
    uploads = [
        UploadedDocument(
            filename=item.filename or "document",
            content=await item.read(),
            content_type=item.content_type,
        )
        for item in incoming
    ]
    try:
        snapshot = service.create_and_start(uploads, parsed_context)
    except TaskError as exc:
        raise _http_error(exc) from exc
    return snapshot.model_dump(mode="json")


@router.get("/tasks")
async def list_tasks(service: TaskService = Depends(get_task_service)) -> list[dict[str, Any]]:
    return [task.model_dump(mode="json") for task in service.list_tasks()]


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    try:
        return service.get_snapshot(task_id).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/result")
async def get_result(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    try:
        return service.get_result(task_id)
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.get("/tasks/{task_id}/events/history")
async def get_event_history(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> list[dict[str, Any]]:
    try:
        service.get_snapshot(task_id)
    except TaskError as exc:
        raise _http_error(exc) from exc
    return [event.model_dump(mode="json") for event in service.read_events(task_id)]


@router.get("/tasks/{task_id}/events")
async def stream_events(
    task_id: str,
    request: Request,
    service: TaskService = Depends(get_task_service),
) -> StreamingResponse:
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


@router.get("/tasks/{task_id}/pages/{index}")
async def get_page_image(
    task_id: str,
    index: int,
    service: TaskService = Depends(get_task_service),
) -> FileResponse:
    try:
        path = service.page_image(task_id, index)
    except TaskError as exc:
        raise _http_error(exc) from exc
    return FileResponse(path, media_type="image/png")


@router.get("/tasks/{task_id}/submission")
async def get_submission(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    try:
        return {"submission": service.get_submission(task_id)}
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/submit")
async def submit_task(
    task_id: str,
    payload: SubmitRequest,
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    try:
        return service.submit(task_id, payload).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/pause")
async def pause_task(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """暂停在途任务：已完成的节点产物保留，可继续。"""

    try:
        return service.pause_task(task_id).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: str, service: TaskService = Depends(get_task_service)
) -> dict[str, Any]:
    """从暂停处继续：只执行没有落盘产物的节点。"""

    try:
        return service.resume_task(task_id).model_dump(mode="json")
    except TaskError as exc:
        raise _http_error(exc) from exc


@router.post("/tasks/{task_id}/cancel")
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


@router.get("/fields")
async def field_catalog() -> dict[str, list[dict[str, Any]]]:
    """分单与舱单共用同一套 AWB 模板字段。"""

    return {
        "house": _spec_catalog(HOUSE_FIELD_SPECS),
        "manifest": _spec_catalog(MANIFEST_FIELD_SPECS),
    }
