"""上传契约：一个文件一个任务，接口只接受单个 file 字段。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def _tasks_url() -> str:
    return f"{get_settings().api_prefix}/analysis/tasks"


def test_upload_rejects_multi_file_field() -> None:
    """旧的 files（多值）字段不再被接受：请求里没有 file 就是 422。"""

    client = TestClient(create_app())
    response = client.post(
        _tasks_url(),
        files=[("files", ("a.pdf", b"%PDF-1.4 test", "application/pdf"))],
    )

    assert response.status_code == 422


def test_upload_requires_file_field() -> None:
    """完全不带文件时同样是 422，而不是建出一个空任务。"""

    client = TestClient(create_app())
    response = client.post(_tasks_url(), data={"context": "{}"})

    assert response.status_code == 422
