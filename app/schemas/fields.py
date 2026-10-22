"""抽取字段的唯一权威定义。

两套字段目录：
- `HOUSE_FIELD_SPECS`：分单（HAWB）单证模板字段，按 AWB 版面分组：
  运输 6、发货人 10、收货人 10、申报 2、制单/货物 8，共 36 项。
  件数/重量/体积只有制单口径（zdpiece/zdweight/zdvolume），不再重复抽取；
  By First Carrier（航班二字码）不抽，航班只保留 hbh（Flight No.）。
- `MANIFEST_FIELD_SPECS`：舱单只保留核对字段（分单号 + 目的港 + 件数/重量/计费重/体积）。

`key` 同时用于：LLM 输出 JSON 的键、核对结果的键、前端表格列。
"""

from dataclasses import dataclass
from typing import Literal

FieldKind = Literal["text", "port", "flight", "date", "integer", "number", "phone", "email"]
FieldGroup = Literal["shipment", "shipper", "consignee", "declaration", "cargo"]

# 参与人（发货人 / 收货人）统一的 10 个字段
PARTY_FIELD_NAMES: tuple[str, ...] = (
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
)
PARTY_LABELS: dict[str, str] = {
    "name": "名称",
    "address": "地址",
    "city": "城市名称",
    "postal_code": "邮编",
    "country": "国家二字码",
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

# 参与人字段 → 单证接口字段名（发货人 fhr / 收货人 shr）
PARTY_INTERFACE_KEYS: dict[str, dict[str, str]] = {
    "shipper": {
        "name": "companytitle_fhr_hawb",
        "address": "address_fhr_hawb",
        "city": "city_fhr_hawb",
        "postal_code": "postcode_fhr_hawb",
        "country": "companycode_fhr_hawb",
        "state": "state_fhr_hawb",
        "contact": "lxr_fhr_hawb",
        "phone": "phone_fhr_hawb",
        "email": "email_fhr_hawb",
        "fax": "fax_fhr_hawb",
    },
    "consignee": {
        "name": "companytitle_shr_hawb",
        "address": "address_shr_hawb",
        "city": "city_shr_hawb",
        "postal_code": "postcode_shr_hawb",
        "country": "companycode_shr_hawb",
        "state": "state_shr_hawb",
        "contact": "lxr_shr_hawb",
        "phone": "phone_shr_hawb",
        "email": "email_shr_hawb",
        "fax": "fax_shr_hawb",
    },
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
    label_prefix = PARTY_GROUP_LABELS[group].split(" ")[0]
    return tuple(
        FieldSpec(
            PARTY_INTERFACE_KEYS[group][name],
            f"{label_prefix}{PARTY_LABELS[name]}",
            group,  # type: ignore[arg-type]
            "phone" if name == "phone" else "email" if name == "email" else "text",
        )
        for name in PARTY_FIELD_NAMES
    )



# ---- 分单模板字段（AWB 版面） ---------------------------------------------
# key 与《接口字段对照表》的「单证」列一一对应（sfg/mdg/hbh/hbrq…）
SHIPMENT_TEMPLATE_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("hawb", "分单号", "shipment", "text"),
    FieldSpec("sfg", "始发港", "shipment", "port"),
    FieldSpec("mdg", "目的港", "shipment", "port"),
    FieldSpec("hbh", "航班号", "shipment", "flight"),
    FieldSpec("hbrq", "航班日期", "shipment", "date"),
    FieldSpec("currency", "币种（Currency）", "shipment", "text"),
)

DECLARATION_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "declarecustom",
        "申报价值（Declared Value for Customs）",
        "declaration",
        "text",
    ),
    FieldSpec("handlinginfo", "操作信息（Handling Information）", "declaration", "text"),
)

CARGO_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("zdpiece", "制单件数（No. of Pieces RCP）", "cargo", "integer"),
    FieldSpec("zdweight", "制单重量（Gross Weight）", "cargo", "number"),
    FieldSpec("ratelevel", "运价类别（Rate Class）", "cargo", "text"),
    FieldSpec("jfweight", "计费重量（Chargeable Weight）", "cargo", "number"),
    FieldSpec("rate", "单价（Rate/Charge）", "cargo", "number"),
    FieldSpec("ratetotal", "总价（Rate/Total）", "cargo", "number"),
    FieldSpec("zdvolume", "制单体积（Volume）", "cargo", "number"),
    FieldSpec("pm", "品名（Nature and Quantity of Goods）", "cargo", "text"),
)

HOUSE_FIELD_SPECS: tuple[FieldSpec, ...] = (
    *SHIPMENT_TEMPLATE_SPECS,
    *_party_specs("shipper"),
    *_party_specs("consignee"),
    *DECLARATION_SPECS,
    *CARGO_SPECS,
)

HOUSE_FIELD_KEYS: tuple[str, ...] = tuple(spec.key for spec in HOUSE_FIELD_SPECS)
FIELD_LABELS: dict[str, str] = {spec.key: spec.label for spec in HOUSE_FIELD_SPECS}

# 舱单只抽取核对用到的字段：分单号（匹配主键）+ 目的港 + 件数/重量/计费重/体积
MANIFEST_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("hawb_no", "分单号", "shipment", "text"),
    FieldSpec("dest_port", "目的港", "shipment", "port"),
    FieldSpec("pieces", "件数", "shipment", "integer"),
    FieldSpec("weight", "重量", "shipment", "number"),
    FieldSpec("chargeable_weight", "计费重量", "cargo", "number"),
    FieldSpec("volume", "体积", "shipment", "number"),
)
# 核对口径 = 舱单字段去掉匹配主键
COMPARE_FIELD_SPECS: tuple[FieldSpec, ...] = tuple(
    spec for spec in MANIFEST_FIELD_SPECS if spec.key != "hawb_no"
)
# 核对字段（舱单口径）→ 分单字段（单证接口名）：只有对不齐名字的才需要映射
HOUSE_KEY_BY_COMPARE_KEY: dict[str, str] = {
    "hawb_no": "hawb",
    "dest_port": "mdg",
    "pieces": "zdpiece",
    "weight": "zdweight",
    "chargeable_weight": "jfweight",
    "volume": "zdvolume",
}
MANIFEST_FIELD_KEYS: tuple[str, ...] = tuple(spec.key for spec in MANIFEST_FIELD_SPECS)

# 抽取结果里必须尽量有值的核心字段（缺失才提示）
CORE_HOUSE_KEYS: tuple[str, ...] = (
    "hawb",
    "sfg",
    "mdg",
    "hbh",
    "hbrq",
    "zdpiece",
    "zdweight",
)


# LLM 输出的 JSON 结构：全部字段平铺，key 即单证接口字段名
HOUSE_BILL_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        spec.key: {"type": ["string", "null"], "description": spec.label}
        for spec in HOUSE_FIELD_SPECS
    },
    "required": [spec.key for spec in HOUSE_FIELD_SPECS],
}

# 舱单只输出核对字段，结构简单平铺
MANIFEST_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        spec.key: {"type": ["string", "null"], "description": spec.label}
        for spec in MANIFEST_FIELD_SPECS
    },
    "required": [spec.key for spec in MANIFEST_FIELD_SPECS],
}


def empty_field_map(keys: tuple[str, ...]) -> dict[str, str | None]:
    """返回指定字段集齐全、值均为 None 的字典（保证前端列固定）。"""

    return {key: None for key in keys}


def flatten_party_fields(
    payload: dict[str, object],
    *,
    keys: tuple[str, ...],
    parties: tuple[str, ...] = ("shipper", "consignee"),
) -> dict[str, str | None]:
    """把嵌套的参与人对象拍平成扁平字段字典。

    `keys` 传 `HOUSE_FIELD_KEYS`（分单）或 `MANIFEST_FIELD_KEYS`（舱单）。
    """

    flat = empty_field_map(keys)
    key_set = set(keys)
    party_keys = {
        PARTY_INTERFACE_KEYS.get(party, {}).get(name, f"{party}_{name}")
        for party in parties
        for name in PARTY_FIELD_NAMES
    }
    for key in keys:
        if key in party_keys:
            continue
        value = payload.get(key)
        flat[key] = None if value is None else str(value).strip() or None
    for party in parties:
        nested = payload.get(party)
        if not isinstance(nested, dict):
            continue
        for name in PARTY_FIELD_NAMES:
            key = PARTY_INTERFACE_KEYS.get(party, {}).get(name, f"{party}_{name}")
            if key not in key_set:
                continue
            value = nested.get(name)
            flat[key] = None if value is None else str(value).strip() or None
    return flat
