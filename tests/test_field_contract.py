"""前后端字段契约：后端抽取的字段必须和前端单证模板一一对应。

前端 constants.js 是手写 key，最容易和后端脱节；这里直接解析源码做集合比对。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.schemas.fields import HOUSE_FIELD_KEYS, HOUSE_FIELD_SPECS, PARTY_INTERFACE_KEYS

CONSTANTS = Path("app/static/constants.js")
PARTY_NAMES = {
    "name",
    "address",
    "city",
    "postal_code",
    "country",
    "state",
    "contact",
    "phone",
    "email",
    "fax",
}


def _slice(start: str, end: str) -> str:
    text = CONSTANTS.read_text(encoding="utf-8")
    begin = text.index(start)
    stop = text.index(end, begin + len(start))
    return text[begin:stop]


def _keys(block: str) -> set[str]:
    return set(re.findall(r'key: "([a-z_0-9]+)"', block))


def _default_house_keys() -> list[str]:
    block = _slice("export const DEFAULT_HOUSE_FIELDS", "export const DEFAULT_MANIFEST_FIELDS")
    return re.findall(r'\["([a-z_0-9]+)",', block)


def _template_keys() -> set[str]:
    template = _slice("export const HOUSE_TEMPLATE", "export const MANIFEST_GROUPS")
    party = _slice("const PARTY_TEMPLATE_FIELDS", "export const HOUSE_TEMPLATE")
    return _keys(template) | _keys(party)


def test_backend_house_field_count() -> None:
    assert len(HOUSE_FIELD_KEYS) == 36
    assert len(set(HOUSE_FIELD_KEYS)) == 36
    # 件数/重量/体积只保留制单口径；By First Carrier 不再抽取
    for removed in ("pieces", "weight", "volume", "first_carrier_code"):
        assert removed not in HOUSE_FIELD_KEYS
    assert "hbh" in HOUSE_FIELD_KEYS


def test_frontend_default_fields_match_backend() -> None:
    keys = _default_house_keys()
    assert len(keys) == len(HOUSE_FIELD_KEYS) == 36
    assert set(keys) == set(HOUSE_FIELD_KEYS)


def test_house_required_fields_are_all_but_party() -> None:
    """分单必填 = 除收/发货人信息外的全部字段（前端按 group 派生，不写死列表）。"""

    text = CONSTANTS.read_text(encoding="utf-8")
    assert "export const REQUIRED_KEYS = DEFAULT_HOUSE_FIELDS.filter(" in text
    assert 'field.group !== "shipper"' in text
    assert 'field.group !== "consignee"' in text

    required = [
        spec for spec in HOUSE_FIELD_SPECS if spec.group not in {"shipper", "consignee"}
    ]
    assert len(required) == 16
    keys = {spec.key for spec in required}
    assert {"hawb", "currency", "declarecustom", "handlinginfo", "zdvolume", "pm"} <= keys
    assert not any(key.endswith(("_fhr_hawb", "_shr_hawb")) for key in keys)


def test_frontend_template_covers_backend_fields() -> None:
    template = _template_keys()
    assert template >= PARTY_NAMES

    party_keys = {
        key for mapping in PARTY_INTERFACE_KEYS.values() for key in mapping.values()
    }
    expanded = (template - PARTY_NAMES) | party_keys

    assert expanded == set(HOUSE_FIELD_KEYS)
