"""字段坐标接口：只给必要内容，坐标换算成相对页序 + 千分比整数。"""

from __future__ import annotations

from app.schemas.file import UploadedDocument
from tests.conftest import make_service

RESULT = {
    "pages": [{"index": 1}, {"index": 2}, {"index": 3}],
    "house_bills": [
        {
            "bill_id": "bill_001",
            "page_indexes": [2],
            "fields": {"hawb": "HAWB0001", "sfg": "PVG"},
            "field_locations": {
                "hawb": [{"page": 2, "bbox": [0.623, 0.08, 0.221, 0.031]}],
                # 越界值要被裁剪到 0~1000
                "sfg": [{"page": 2, "bbox": [-0.2, 1.5, 0.16, 0.02]}],
            },
        },
        {
            "bill_id": "bill_002",
            "page_indexes": [1, 3],
            "fields": {"hawb": "HAWB0002"},
            "field_locations": {"hawb": [{"page": 3, "bbox": [0.1, 0.2, 0.3, 0.4]}]},
        },
        {
            # 完全没有坐标的分单不出现在结果里
            "bill_id": "bill_003",
            "page_indexes": [1],
            "fields": {},
            "field_locations": {},
        },
    ],
}


def _prepare(settings):
    service = make_service(settings)
    snapshot = service.create_task(
        UploadedDocument(filename="demo.pdf", content=b"%PDF-1.4 demo")
    )
    service.store.write_json_atomic(service.store.result_path(snapshot.task_id), RESULT)
    return service, snapshot.task_id


def test_locations_keep_only_relative_pages_and_integers(settings) -> None:
    service, task_id = _prepare(settings)
    data = service.house_bill_locations(task_id)

    assert data["schema_version"] == 1
    assert data["task_id"] == task_id
    assert [bill["id"] for bill in data["bills"]] == ["bill_001", "bill_002"]

    first = data["bills"][0]
    assert first["off"] == 2
    assert first["loc"]["hawb"] == [[1, 623, 80, 221, 31]]
    assert first["loc"]["sfg"] == [[1, 0, 1000, 160, 20]]


def test_locations_map_task_page_to_bill_page(settings) -> None:
    """任务第 3 页是 bill_002 的第 2 页 → 相对页序必须是 2，否则 ERP 归档图对不上。"""

    service, task_id = _prepare(settings)
    data = service.house_bill_locations(task_id)

    second = data["bills"][1]
    assert second["off"] == 1
    assert second["loc"]["hawb"] == [[2, 100, 200, 300, 400]]


def test_locations_route_returns_payload(settings) -> None:
    from fastapi.testclient import TestClient

    from app.api import dependencies
    from app.config import get_settings
    from app.main import create_app

    service, task_id = _prepare(settings)
    app = create_app()
    app.dependency_overrides[dependencies.get_task_service] = lambda: service
    client = TestClient(app)

    response = client.get(
        f"{get_settings().api_prefix}/analysis/tasks/{task_id}/locations"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["bills"][0]["loc"]["hawb"] == [[1, 623, 80, 221, 31]]
