"""抽取字段的唯一权威定义。

两套字段目录：
- `FIELD_SPECS`：早期 16 项基线（保留兼容用），核对与舱单已改用完整字段集。
- `HOUSE_FIELD_SPECS`：分单（HAWB）单证模板字段，按 AWB 版面分组：
  运输 10、发货人 9、收货人 9、申报 2、制单/货物 8，共 38 项。
- 舱单沿用同一套字段（`MANIFEST_FIELD_SPECS = HOUSE_FIELD_SPECS`），保证可逐字段核对。

`key` 同时用于：LLM 输出 JSON 的键、核对结果的键、前端表格列。
"""

from dataclasses import dataclass
from typing import Literal

FieldKind = Literal["text", "port", "flight", "date", "integer", "number", "phone", "email"]
FieldGroup = Literal["shipment", "shipper", "consignee", "declaration", "cargo"]

# 参与人（发货人 / 收货人）统一的 9 个字段
PARTY_FIELD_NAMES: tuple[str, ...] = (
    "name",
    "address",
    "city",
    "postal_code",
    "state",
    "contact",
    "phone",
    "email",
    "fax",
)
PARTY_LABELS: dict[str, str] = {
    "name": "名称",
    "address": "地址",
    "city": "城市名称",
    "postal_code": "邮编",
    "state": "州名",
    "contact": "联系人",
    "phone": "手机号",
    "email": "邮箱",
    "fax": "传真",
}
PARTY_GROUPS: tuple[str, ...] = ("shipper", "consignee")
PARTY_GROUP_LABELS: dict[str, str] = {
    "shipper": "发货人 SHIPPER",
    "consignee": "收货人 CONSIGNEE",
}


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    group: FieldGroup
    kind: FieldKind
    # 核对时允许的相对误差（数值字段），None 表示精确比较
    tolerance: float | None = None


def _party_specs(group: str) -> tuple[FieldSpec, ...]:
    prefix = group
    label_prefix = PARTY_GROUP_LABELS[group].split(" ")[0]
    return tuple(
        FieldSpec(
            f"{prefix}_{name}",
            f"{label_prefix}{PARTY_LABELS[name]}",
            group,  # type: ignore[arg-type]
            "phone" if name == "phone" else "email" if name == "email" else "text",
        )
        for name in PARTY_FIELD_NAMES
    )


# ---- 分单 / 舱单共有字段（核对口径） --------------------------------------
FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("hawb_no", "分单号", "shipment", "text"),
    FieldSpec("origin_port", "始发港", "shipment", "port"),
    FieldSpec("dest_port", "目的港", "shipment", "port"),
    FieldSpec("flight_no", "航班号", "shipment", "flight"),
    FieldSpec("flight_date", "航班日期", "shipment", "date"),
    FieldSpec("pieces", "件数", "shipment", "integer"),
    FieldSpec("weight", "重量", "shipment", "number", tolerance=0.05),
    FieldSpec("volume", "体积", "shipment", "number", tolerance=0.05),
    FieldSpec("shipper_name", "发货人名称", "shipper", "text"),
    FieldSpec("shipper_address", "发货人地址", "shipper", "text"),
    FieldSpec("shipper_phone", "发货人电话", "shipper", "phone"),
    FieldSpec("shipper_email", "发货人邮箱", "shipper", "email"),
    FieldSpec("consignee_name", "收货人名称", "consignee", "text"),
    FieldSpec("consignee_address", "收货人地址", "consignee", "text"),
    FieldSpec("consignee_phone", "收货人电话", "consignee", "phone"),
    FieldSpec("consignee_email", "收货人邮箱", "consignee", "email"),
)



# ---- 分单模板字段（AWB 版面） ---------------------------------------------
SHIPMENT_TEMPLATE_SPECS: tuple[FieldSpec, ...] = (
    *FIELD_SPECS[:8],
    FieldSpec("first_carrier_code", "航班二字码（By First Carrier）", "shipment", "text"),
    FieldSpec("currency", "币种（Currency）", "shipment", "text"),
)

DECLARATION_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("declared_value_carriage", "声明价值（Declared Value for Carriage）", "declaration", "text"),
    FieldSpec("handling_info", "操作信息（Handling Information）", "declaration", "text"),
)

CARGO_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("rcp_pieces", "制单件数（No. of Pieces RCP）", "cargo", "integer"),
    FieldSpec("rcp_gross_weight", "制单重量（Gross Weight）", "cargo", "number"),
    FieldSpec("rate_class", "运价类别（Rate Class）", "cargo", "text"),
    FieldSpec("chargeable_weight", "计费重量（Chargeable Weight）", "cargo", "number"),
    FieldSpec("rate_charge", "单价（Rate/Charge）", "cargo", "number"),
    FieldSpec("rate_total", "总价（Rate/Total）", "cargo", "number"),
    FieldSpec("rcp_volume", "制单体积（Volume）", "cargo", "number"),
    FieldSpec("goods_description", "品名（Nature and Quantity of Goods）", "cargo", "text"),
)

HOUSE_FIELD_SPECS: tuple[FieldSpec, ...] = (
    *SHIPMENT_TEMPLATE_SPECS,
    *_party_specs("shipper"),
    *_party_specs("consignee"),
    *DECLARATION_SPECS,
    *CARGO_SPECS,
)

FIELD_SPEC_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in HOUSE_FIELD_SPECS}
HOUSE_FIELD_BY_KEY: dict[str, FieldSpec] = FIELD_SPEC_BY_KEY
FIELD_KEYS: tuple[str, ...] = tuple(spec.key for spec in FIELD_SPECS)
HOUSE_FIELD_KEYS: tuple[str, ...] = tuple(spec.key for spec in HOUSE_FIELD_SPECS)
FIELD_LABELS: dict[str, str] = {spec.key: spec.label for spec in HOUSE_FIELD_SPECS}

# 舱单字段与分单保持一致，核对时逐字段比较
MANIFEST_FIELD_SPECS: tuple[FieldSpec, ...] = HOUSE_FIELD_SPECS
COMPARE_FIELD_SPECS: tuple[FieldSpec, ...] = HOUSE_FIELD_SPECS
MANIFEST_FIELD_KEYS: tuple[str, ...] = HOUSE_FIELD_KEYS

SHIPMENT_FIELD_KEYS: tuple[str, ...] = tuple(
    spec.key for spec in FIELD_SPECS if spec.group == "shipment"
)
SHIPPER_FIELD_KEYS: tuple[str, ...] = tuple(
    spec.key for spec in HOUSE_FIELD_SPECS if spec.group == "shipper"
)
CONSIGNEE_FIELD_KEYS: tuple[str, ...] = tuple(
    spec.key for spec in HOUSE_FIELD_SPECS if spec.group == "consignee"
)
PARTY_FIELD_KEYS: tuple[str, ...] = SHIPPER_FIELD_KEYS + CONSIGNEE_FIELD_KEYS

# 抽取结果里必须尽量有值的核心字段（缺失才提示）
CORE_HOUSE_KEYS: tuple[str, ...] = (
    "hawb_no",
    "origin_port",
    "dest_port",
    "flight_no",
    "flight_date",
    "pieces",
    "weight",
)


def _party_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            name: {"type": ["string", "null"], "description": PARTY_LABELS[name]}
            for name in PARTY_FIELD_NAMES
        },
    }


# LLM 输出的 JSON 结构：运输/申报/制单字段平铺，参与人嵌套
HOUSE_BILL_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        **{
            spec.key: {"type": ["string", "null"], "description": spec.label}
            for spec in (*SHIPMENT_TEMPLATE_SPECS, *DECLARATION_SPECS, *CARGO_SPECS)
        },
        **{group: _party_json_schema() for group in PARTY_GROUPS},
    },
    "required": [
        spec.key
        for spec in (*SHIPMENT_TEMPLATE_SPECS, *DECLARATION_SPECS, *CARGO_SPECS)
    ]
    + list(PARTY_GROUPS),
}

# 舱单与分单共用同一套字段结构
MANIFEST_JSON_SCHEMA: dict[str, object] = HOUSE_BILL_JSON_SCHEMA


def empty_field_map(keys: tuple[str, ...] = FIELD_KEYS) -> dict[str, str | None]:
    """返回指定字段集齐全、值均为 None 的字典（保证前端列固定）。"""

    return {key: None for key in keys}


def flatten_party_fields(
    payload: dict[str, object],
    *,
    keys: tuple[str, ...] = FIELD_KEYS,
    parties: tuple[str, ...] = ("shipper", "consignee"),
) -> dict[str, str | None]:
    """把嵌套的参与人对象拍平成扁平字段字典。

    默认参数保持舱单的 16 键行为；分单传入 `HOUSE_FIELD_KEYS` + 三个参与人。
    """

    flat = empty_field_map(keys)
    key_set = set(keys)
    for key in keys:
        if any(key.startswith(f"{party}_") for party in parties):
            continue
        value = payload.get(key)
        flat[key] = None if value is None else str(value).strip() or None
    for party in parties:
        nested = payload.get(party)
        if not isinstance(nested, dict):
            continue
        for name in PARTY_FIELD_NAMES:
            key = f"{party}_{name}"
            if key not in key_set:
                continue
            value = nested.get(name)
            flat[key] = None if value is None else str(value).strip() or None
    return flat
