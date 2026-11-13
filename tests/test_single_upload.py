"""上传契约：单票（files，多文件一个任务）与批量（file，一文件一任务）。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api import dependencies
from app.config import Settings, get_settings
from app.main import create_app
from app.service.task_service import TaskService


def _tasks_url() -> str:
    return f"{get_settings().api_prefix}/analysis/tasks"


def _client(tmp_path: Path) -> TestClient:
    """临时数据目录的服务：上传会真的建任务目录，不能落到仓库 data/ 里。"""

    settings = Settings(
        data_root=tmp_path / "data",
        log_dir=tmp_path / "logs",
        model_api_key=None,
    )
    service = TaskService(settings)
    app = create_app()
    app.dependency_overrides[dependencies.get_task_service] = lambda: service
    return TestClient(app)


def test_single_mode_accepts_multiple_files(tmp_path: Path) -> None:
    """单票：files 多值 + mode=single，合成一个任务。"""

    client = _client(tmp_path)
    response = client.post(
        _tasks_url(),
        files=[
            ("files", ("a.pdf", b"%PDF-1.4 test", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4 test", "application/pdf")),
        ],
        data={"mode": "single"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "single"
    assert len(body["files"]) == 2
    assert "等 2 个文件" in body["source_name"]


def test_batch_mode_rejects_multiple_files(tmp_path: Path) -> None:
    """批量：一个文件一个任务，接口层直接拒绝一次传多个。"""

    client = _client(tmp_path)
    response = client.post(
        _tasks_url(),
        files=[
            ("files", ("a.pdf", b"%PDF-1.4 test", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4 test", "application/pdf")),
        ],
        data={"mode": "batch"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "BATCH_MODE_SINGLE_FILE"


def test_upload_requires_at_least_one_file(tmp_path: Path) -> None:
    """完全不带文件时不建空任务。"""

    client = _client(tmp_path)
    response = client.post(_tasks_url(), data={"context": "{}"})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "FILE_REQUIRED"


def test_upload_rejects_more_than_five_files(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        _tasks_url(),
        files=[
            ("files", (f"a{index}.pdf", b"%PDF-1.4 test", "application/pdf"))
            for index in range(6)
        ],
        data={"mode": "single"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "TOO_MANY_FILES"


def test_upload_rejects_unknown_mode(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        _tasks_url(),
        files=[("files", ("a.pdf", b"%PDF-1.4 test", "application/pdf"))],
        data={"mode": "whatever"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_MODE"


def test_legacy_file_field_still_works(tmp_path: Path) -> None:
    """旧的单文件字段 `file` 保持兼容（批量模式的调用方还在用）。"""

    client = _client(tmp_path)
    response = client.post(
        _tasks_url(),
        files=[("file", ("a.pdf", b"%PDF-1.4 test", "application/pdf"))],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "batch"
    assert len(body["files"]) == 1
