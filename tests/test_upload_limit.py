"""上传数量限制：一次最多 5 个文件。"""

from __future__ import annotations

import pytest

from app.schemas.file import UploadedDocument
from app.service.task_service import TaskError, TaskService


def _upload(name: str) -> UploadedDocument:
    return UploadedDocument(filename=name, content=b"%PDF-1.4 test")


def test_create_task_rejects_more_than_five_files(settings) -> None:
    service = TaskService(settings)
    uploads = [_upload(f"doc{index}.pdf") for index in range(6)]

    with pytest.raises(TaskError) as excinfo:
        service.create_task(uploads)

    assert excinfo.value.code == "FILE_COUNT_EXCEEDED"


def test_create_task_accepts_five_files(settings) -> None:
    service = TaskService(settings)
    uploads = [_upload(f"doc{index}.pdf") for index in range(5)]

    snapshot = service.create_task(uploads)

    assert snapshot.task_id
