"""提交接口：差异确认、幂等与重跑清理。"""

import asyncio

import pytest

from app.schemas.file import UploadedDocument
from app.schemas.submit import SubmitDocument, SubmitRequest
from app.service.task_service import TaskError, TaskService
from tests.conftest import (
    MANIFEST_PAGE,
    MASTER_PAGE,
    house_page,
    make_service,
    write_pdf,
)


async def _run_task(service: TaskService, task_id: str) -> None:
    service.start(task_id)
    for _ in range(200):
        snapshot = service.get_snapshot(task_id)
        if snapshot.status in {"succeeded", "failed"}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("任务未在预期时间内结束")


def _submit_payload(request_id: str, acknowledged: bool) -> SubmitRequest:
    return SubmitRequest(
        request_id=request_id,
        acknowledged=acknowledged,
        documents=[
            SubmitDocument(
                tab_id="bill_001",
                kind="house_bill",
                fields={"hawb_no": "MOCK0001", "weight": "186.5"},
            ),
            SubmitDocument(
                tab_id="manifest",
                kind="manifest",
                entries=[{"entry_id": "mft_0001", "fields": {"hawb_no": "MOCK0001"}}],
            ),
        ],
    )


async def test_submit_requires_acknowledgement_when_inconsistent(settings, tmp_path):
    pdf = write_pdf(
        tmp_path / "bundle.pdf",
        [MASTER_PAGE, house_page("HAWB0001"), house_page("HAWB0002"), MANIFEST_PAGE],
    )
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="bundle.pdf", content=pdf.read_bytes())
    )
    await _run_task(service, snapshot.task_id)
    assert service.get_snapshot(snapshot.task_id).inconsistent_count == 1

    with pytest.raises(TaskError) as excinfo:
        service.submit(snapshot.task_id, _submit_payload("req-1", acknowledged=False))
    assert excinfo.value.code == "DIFF_NOT_ACKNOWLEDGED"

    response = service.submit(snapshot.task_id, _submit_payload("req-1", acknowledged=True))
    assert response.ok is True
    assert response.submission_code.startswith("DZ")
    assert response.duplicated is False

    submitted = service.get_snapshot(snapshot.task_id)
    assert submitted.submitted is True
    assert submitted.submission_code == response.submission_code

    record = service.get_submission(snapshot.task_id)
    assert record is not None
    assert record["request_id"] == "req-1"
    assert record["documents"][0]["fields"]["weight"] == "186.5"

    # 同一 request_id 重放：返回首次结果，不重复落盘
    replay = service.submit(snapshot.task_id, _submit_payload("req-1", acknowledged=True))
    assert replay.duplicated is True
    assert replay.submission_code == response.submission_code


def test_submit_rejects_unfinished_task(settings, tmp_path):
    pdf = write_pdf(tmp_path / "x.pdf", [MASTER_PAGE])
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="x.pdf", content=pdf.read_bytes())
    )
    with pytest.raises(TaskError) as excinfo:
        service.submit(snapshot.task_id, _submit_payload("req-3", acknowledged=True))
    assert excinfo.value.code == "TASK_NOT_READY"
