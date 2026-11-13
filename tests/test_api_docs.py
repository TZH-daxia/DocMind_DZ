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
    # 三个字段的描述都要是可读中文：单票的 files、兼容的 file、识别方式 mode
    assert "批量模式" in form["properties"]["file"]["description"]
    assert "单票" in form["properties"]["files"]["description"]
    assert "single=单票" in form["properties"]["mode"]["description"]

    submit = spec["components"]["schemas"]["SubmitRequest"]["properties"]
    assert "待提交的页签" in submit["documents"]["description"]
    assert "幂等请求 ID" in submit["request_id"]["description"]


def test_upload_fields_use_binary_format() -> None:
    """上传字段要输出 format: binary，Swagger UI 才会渲染文件框而不是乱码字符串。"""

    client = TestClient(create_app())
    spec = client.get("/openapi.json").json()

    form = _upload_form_schema(spec)

    # 单票的 files（数组）与批量的 file（单值）都要输出 binary，
    # 否则 Swagger UI 会把上传字段渲染成普通字符串并预填乱码
    assert '"format": "binary"' in json.dumps(form["properties"]["file"])
    assert '"format": "binary"' in json.dumps(form["properties"]["files"])
    # OAS 3.1 的 contentMediaType 会让 Swagger UI 预填随机二进制
    assert "contentMediaType" not in json.dumps(spec)
