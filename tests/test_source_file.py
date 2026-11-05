"""原件下载：任务状态里的 uploaded_path 是唯一权威来源。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies
from app.config import get_settings
from app.main import create_app
from app.schemas.file import UploadedDocument
from app.service.task_service import TaskError
from tests.conftest import make_service


def test_source_document_returns_uploaded_bytes(settings) -> None:
    """按任务取原件：字节与原始文件名都来自创建任务时写入的状态。"""

    service = make_service(settings)
    content = b"%PDF-1.4 source-file-demo"
    upload = UploadedDocument(
        filename="单证 测试.pdf",
        content=content,
        content_type="application/pdf",
    )

    snapshot = service.create_task(upload)
    path, name = service.source_document(snapshot.task_id)

    assert path.exists()
    assert path.read_bytes() == content
    assert name == "单证 测试.pdf"


def test_source_document_missing_task(settings) -> None:
    """任务不存在（或产物已被清理）时抛 TaskError，路由层转成 404。"""

    service = make_service(settings)

    with pytest.raises(TaskError):
        service.source_document("task_not_exists")


def test_file_route_streams_source_with_filename(settings) -> None:
    """HTTP 层：原样返回字节，并把文件名放进 Content-Disposition。"""

    service = make_service(settings)
    content = b"%PDF-1.4 route-demo"
    upload = UploadedDocument(filename="demo.pdf", content=content, content_type="application/pdf")
    snapshot = service.create_task(upload)

    app = create_app()
    app.dependency_overrides[dependencies.get_task_service] = lambda: service
    client = TestClient(app)

    response = client.get(
        f"{get_settings().api_prefix}/analysis/tasks/{snapshot.task_id}/file"
    )

    assert response.status_code == 200
    assert response.content == content
    assert "attachment" in response.headers["content-disposition"]
    assert "demo.pdf" in response.headers["content-disposition"]
