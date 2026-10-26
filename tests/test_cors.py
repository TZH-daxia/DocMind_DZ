"""跨域配置：开发开关放开全部来源，默认仍按白名单。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _preflight(origin: str) -> str | None:
    client = TestClient(create_app())
    response = client.options(
        "/docmind_dz/analysis/tasks",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    return response.headers.get("access-control-allow-origin")


def test_allow_all_origins_when_dev_switch_on(monkeypatch) -> None:
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(cors_allow_all=True))

    assert _preflight("http://172.16.5.9:9000") == "*"


def test_unknown_origin_blocked_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.get_settings", lambda: Settings(cors_allow_all=False)
    )

    assert _preflight("http://172.16.5.9:9000") is None
