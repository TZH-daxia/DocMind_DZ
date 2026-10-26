"""接口文档：编码与上传字段的 OpenAPI 描述。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app


def _upload_form_schema(spec: dict) -> dict:
    """按关键字定位上传请求体 schema：operation id 会随接口前缀变化。"""

    for name, schema in spec["components"]["schemas"].items():
        if "create_task" in name and name.startswith("Body_"):
            return schema
    raise AssertionError("未找到上传请求体 schema")


def test_openapi_json_declares_utf8_charset() -> None:
    client = TestClient(create_app())
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "charset=utf-8" in response.headers["content-type"].lower()


def test_post_field_descriptions_are_readable() -> None:
    """POST 接口的必填/表单字段描述必须是正常中文，不能是双重编码的乱码。"""

    client = TestClient(create_app())
    spec = client.get("/openapi.json").json()

    form = _upload_form_schema(spec)
    assert "单证文件" in form["properties"]["files"]["description"]

    submit = spec["components"]["schemas"]["SubmitRequest"]["properties"]
    assert "待提交的页签" in submit["documents"]["description"]
    assert "幂等请求 ID" in submit["request_id"]["description"]


def test_upload_fields_use_binary_format() -> None:
    """上传字段要输出 format: binary，Swagger UI 才会渲染文件框而不是乱码字符串。"""

    client = TestClient(create_app())
    spec = client.get("/openapi.json").json()

    form = _upload_form_schema(spec)
    files_schema = form["properties"]["files"]

    assert files_schema["anyOf"][0]["items"] == {"type": "string", "format": "binary"}
    assert form["properties"]["file"]["anyOf"][0]["format"] == "binary"
    # OAS 3.1 的 contentMediaType 会让 Swagger UI 预填随机二进制
    assert "contentMediaType" not in json.dumps(spec)
