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


def test_single_mode_lists_and_streams_all_sources(settings) -> None:
    """单票多文件：清单给出全部原件，按 index 能逐个下载到对应字节。"""

    service = make_service(settings)
    snapshot = service.create_task(
        [
            UploadedDocument(filename="a.pdf", content=b"%PDF-1.4 first"),
            UploadedDocument(filename="b.pdf", content=b"%PDF-1.4 second"),
        ],
        mode="single",
    )

    app = create_app()
    app.dependency_overrides[dependencies.get_task_service] = lambda: service
    client = TestClient(app)
    base = f"{get_settings().api_prefix}/analysis/tasks/{snapshot.task_id}"

    listing = client.get(f"{base}/files")
    assert listing.status_code == 200
    items = listing.json()
    assert [item["index"] for item in items] == [1, 2]
    assert [item["name"] for item in items] == ["a.pdf", "b.pdf"]

    second = client.get(f"{base}/files/2")
    assert second.status_code == 200
    assert second.content == b"%PDF-1.4 second"
    assert "b.pdf" in second.headers["content-disposition"]

    # 老接口仍只返回第一个，保持兼容
    legacy = client.get(f"{base}/file")
    assert legacy.content == b"%PDF-1.4 first"


def test_single_mode_keeps_duplicate_names_apart(settings) -> None:
    """同名单证不会互相覆盖：落盘改名，清单里也是两个不同的名字。"""

    service = make_service(settings)
    snapshot = service.create_task(
        [
            UploadedDocument(filename="a.pdf", content=b"%PDF-1.4 one"),
            UploadedDocument(filename="a.pdf", content=b"%PDF-1.4 two"),
        ],
        mode="single",
    )

    items = service.source_documents(snapshot.task_id)
    assert len(items) == 2
    assert items[0][0] != items[1][0]
    assert [name for _, name in items] == ["a.pdf", "a_2.pdf"]


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
