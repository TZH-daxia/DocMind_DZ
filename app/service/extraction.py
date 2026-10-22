"""把模型输出转成领域字段的适配层。

职责边界：只做「拼提示词 → 调模型 → 解析 JSON → 按模板拍平字段」，不做页面分段、
不做核对。页面分段在 collector，核对在 consistency。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agent.parsing import (
    JsonParseError,
    first_json_char,
    parse_array,
    parse_object,
)
from app.prompts.loader import (
    load_house_bill_extraction_prompt,
    load_manifest_extraction_prompt,
    load_page_transcription_prompt,
    load_segment_classification_prompt,
)
from app.schemas.documents import PageRecord
from app.schemas.fields import (
    CORE_HOUSE_KEYS,
    HOUSE_FIELD_KEYS,
    HOUSE_KEY_BY_COMPARE_KEY,
    MANIFEST_FIELD_KEYS,
    PARTY_FIELD_NAMES,
    PARTY_GROUPS,
    PARTY_INTERFACE_KEYS,
    flatten_party_fields,
)
from app.service.normalizers import normalize_phone
from app.service.ports import normalize_port_fields

logger = logging.getLogger(__name__)

PAGE_HEADER = "<<<PAGE {index}>>>"
MAX_PAGE_CHARS = 6000

# 标准键保持「舱单 / 核对口径」；分单接口名（mdg/zdpiece…）作为别名，
# 分单归一化时再统一改写成接口字段名，避免舱单字段被分单命名吃掉。
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "hawb_no": ("hawb", "hawbno", "hawb_number", "house_no", "house_bill_no", "分单号"),
    "sfg": ("origin_port", "origin", "pol", "origin_airport", "始发港", "起运港"),
    "dest_port": ("mdg", "destination", "dest", "pod", "destination_airport", "目的港"),
    "hbh": ("flight_no", "flight", "flight_number", "flt", "航班号"),
    "hbrq": ("flight_date", "date", "etd", "departure_date", "航班日期"),
    "pieces": ("zdpiece", "rcp_pieces", "pieces_rcp", "pcs", "piece", "件数"),
    "weight": ("zdweight", "rcp_gross_weight", "rcp_weight", "gross_weight", "重量", "毛重"),
    "volume": ("zdvolume", "rcp_volume", "declared_volume", "cbm", "体积"),
    "currency": ("currency_code", "ccy", "币种", "货币"),
    "declarecustom": (
        "declared_value_carriage",
        "declared_value",
        "dvc",
        "declared_value_for_carriage",
        "申报价值",
        "声明价值",
    ),
    "handlinginfo": ("handling_info", "handling_information", "handling", "操作信息", "处理信息"),
    "chargeable_weight": ("jfweight", "chargeable", "chg_weight", "cw", "计费重量"),
    "ratelevel": ("rate_class", "rateclass", "class", "运价类别"),
    "rate": ("rate_charge", "unit_price", "unitprice", "单价"),
    "ratetotal": ("rate_total", "total", "total_charge", "amount", "总价", "总额"),
    "pm": (
        "goods_description",
        "goods",
        "nature_and_quantity_of_goods",
        "description",
        "品名",
        "货物描述",
    ),
}

NESTED_PARTY_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("company", "company_name", "companyname", "名称"),
    "address": ("addr", "地址"),
    "city": ("city_name", "城市", "城市名称"),
    "postal_code": ("postcode", "zip", "zipcode", "postal", "邮编", "邮政编码"),
    "country": (
        "country_code",
        "countrycode",
        "country_name",
        "nation",
        "国家",
        "国家二字码",
        "国家代码",
    ),
    "state": ("province", "state_name", "州", "州名", "省", "省份"),
    "contact": ("contact_person", "contactperson", "attn", "attention", "联系人"),
    "phone": ("tel", "telephone", "mobile", "phone_no", "cell", "电话", "手机号"),
    "email": ("mail", "e-mail", "邮箱"),
    "fax": ("fax_no", "faxno", "传真", "传真号"),
}

_PARTY_CN = {"shipper": "发货人", "consignee": "收货人"}

PARTY_ALIASES: dict[str, tuple[str, ...]] = {
    f"{party}_{name}": (*alias, PARTY_INTERFACE_KEYS[party][name])
    for party, cn in _PARTY_CN.items()
    for name, alias in (
        ("name", (f"{party}_name", f"{party}_company", f"{party}_company_name", cn, f"{cn}名称")),
        ("address", (f"{party}_address", f"{party}_addr", f"{cn}地址")),
        ("city", (f"{party}_city", f"{party}_city_name", f"{cn}城市", f"{cn}城市名称")),
        ("postal_code", (f"{party}_postal_code", f"{party}_postcode", f"{party}_zip", f"{cn}邮编")),
        (
            "country",
            (
                f"{party}_country",
                f"{party}_country_code",
                f"{party}_companycode",
                f"{cn}国家",
                f"{cn}国家二字码",
            ),
        ),
        ("state", (f"{party}_state", f"{party}_province", f"{cn}州名", f"{cn}省份")),
        (
            "contact",
            (f"{party}_contact", f"{party}_contact_person", f"{party}_attn", f"{cn}联系人"),
        ),
        (
            "phone",
            (f"{party}_phone", f"{party}_tel", f"{party}_mobile", f"{cn}电话", f"{cn}手机号"),
        ),
        ("email", (f"{party}_email", f"{party}_mail", f"{cn}邮箱")),
        ("fax", (f"{party}_fax", f"{party}_fax_no", f"{cn}传真")),
    )
}


class ExtractionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def format_pages(pages: list[PageRecord], indexes: list[int] | None = None) -> str:
    selected = [page for page in pages if indexes is None or page.index in indexes]
    chunks: list[str] = []
    for page in selected:
        text = page.text.strip()
        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS] + "\n…（本页内容过长已截断）"
        chunks.append(PAGE_HEADER.format(index=page.index) + "\n" + (text or "（本页无文本）"))
    return "\n\n".join(chunks)


async def transcribe_page(gateway, page: PageRecord, image_path: str) -> str:
    """VLM 逐字转写单页。"""

    return await gateway.complete_vision(
        "页面图像转写",
        load_page_transcription_prompt(),
        f"请逐字转写第 {page.index} 页的全部内容。",
        [image_path],
    )


async def classify_pages_with_llm(gateway, pages: list[PageRecord]) -> dict[int, dict[str, Any]]:
    """把规则无法判定的页面交给 LLM 分类，返回 {页号: 判定}。"""

    if not pages:
        return {}
    blocks = []
    for page in pages:
        text = page.text.strip() or "（无文本，可能是扫描页）"
        if len(text) > 3000:
            text = text[:3000] + "…"
        blocks.append(f"PAGE {page.index}\n{text}")
    human = "请对以下页面逐页分类：\n\n" + "\n\n".join(blocks)
    response = await gateway.complete(
        "页面分类", load_segment_classification_prompt(), human
    )
    try:
        items = parse_array(response)
    except JsonParseError as exc:
        raise ExtractionError("CLASSIFY_INVALID", f"页面分类结果不是合法 JSON：{exc}") from exc

    result: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("page"))
        except (TypeError, ValueError):
            continue
        kind = str(item.get("kind") or "other")
        if kind not in {"master_bill", "house_bill", "manifest", "other"}:
            kind = "other"
        result[index] = {
            "kind": kind,
            "doc_no": (str(item.get("doc_no")).strip() or None) if item.get("doc_no") else None,
            "continuation": bool(item.get("continuation")),
            "confidence": _to_float(item.get("confidence"), 0.6),
            "reason": str(item.get("reason") or ""),
        }
    return result


async def extract_house_bill(
    gateway, pages: list[PageRecord], indexes: list[int]
) -> dict[str, Any]:
    """抽取一份分单，返回 {fields, confidence, warnings}。"""

    human = (
        "以下是同一份分单（HAWB）的全部页面内容，请按单证模板抽取字段："
        "分单号与运输信息、发货人、收货人、通知人、申报与保险、制单与货物信息。"
        "缺失字段填 null，不要猜测。\n\n" + format_pages(pages, indexes)
    )
    response = await gateway.complete(
        "分单字段抽取", load_house_bill_extraction_prompt(), human
    )
    try:
        payload = parse_object(response)
    except JsonParseError as exc:
        raise ExtractionError("EXTRACT_INVALID", f"分单抽取结果不是合法 JSON：{exc}") from exc
    return _normalize_house_bill_payload(payload)


async def extract_manifest(gateway, pages: list[PageRecord], indexes: list[int]) -> dict[str, Any]:
    """抽取舱单表格，返回 {entries, confidence, warnings}。"""

    human = (
        "以下是舱单（Cargo Manifest）的全部页面内容，请逐行抽取。"
        "每个数据行输出一条记录，行顺序与舱单一致。\n\n" + format_pages(pages, indexes)
    )
    response = await gateway.complete(
        "舱单表格抽取", load_manifest_extraction_prompt(), human
    )
    payload: dict[str, Any] = {}
    try:
        if first_json_char(response) == "[":
            # 兼容模型直接返回数组（没有 {"entries": [...]} 外壳）
            entries = parse_array(response)
        else:
            payload = parse_object(response)
            entries = payload.get("entries")
    except JsonParseError as exc:
        raise ExtractionError(
            "EXTRACT_INVALID", f"舱单抽取结果不是合法 JSON：{exc}"
        ) from exc
    if not isinstance(entries, list):
        raise ExtractionError("EXTRACT_INVALID", "舱单抽取结果缺少 entries 数组")

    master_source: dict[str, Any] | None = (
        payload["master"] if isinstance(payload.get("master"), dict) else None
    )

    normalized_entries: list[dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        row_type = str(entry.get("row_type") or entry.get("type") or "").strip().upper()
        raw_fields = entry.get("fields")
        fields_source = raw_fields if isinstance(raw_fields, dict) else entry
        canonical = canonical_payload(fields_source)
        hawb_no = canonical.get("hawb_no")
        is_master_row = row_type == "M" or (
            isinstance(hawb_no, str) and re.fullmatch(r"\d{3}-?\d{8}", hawb_no.strip())
        )
        if is_master_row:
            # 总运单汇总行：不作为分单行，单独保存用于和分单合计核对
            if master_source is None:
                master_source = fields_source
            continue
        entry_fields = flatten_party_fields(
            canonical, keys=MANIFEST_FIELD_KEYS, parties=()
        )
        normalize_port_fields(entry_fields, keys=("dest_port",))
        normalized_entries.append(
            {
                "line_no": _to_int(entry.get("line_no"), position),
                # 舱单只保留核对字段（分单号 + 目的港 + 件数/重量/计费重/体积）
                "fields": entry_fields,
                "confidence": _to_float(
                    entry.get("confidence"), payload.get("confidence", 0.6)
                ),
                "warnings": _to_warnings(entry.get("warnings")),
            }
        )
    return {
        "entries": normalized_entries,
        "master": _normalize_manifest_master(master_source),
        "confidence": _to_float(payload.get("confidence"), 0.6),
        "warnings": _to_warnings(payload.get("warnings")),
    }


def _normalize_manifest_master(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """把舱单总运单（M 行或页头汇总栏）归一成固定字段。"""

    if not isinstance(payload, dict):
        return None
    lowered = {str(key).strip().lower(): value for key, value in payload.items()}

    def pick(*names: str) -> str | None:
        for name in names:
            value = lowered.get(name)
            if value in (None, ""):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    master = {
        "mawb_no": pick(
            "mawb_no", "mawb", "master_no", "main_no", "hawb_no", "主单号", "总单号"
        ),
        "dest_port": pick("dest_port", "mdg", "destination", "dest", "目的地", "目的港"),
        "pieces": pick("pieces", "zdpiece", "total_pieces", "件数"),
        "weight": pick("weight", "zdweight", "total_weight", "gross_weight", "重量"),
        "chargeable_weight": pick(
            "chargeable_weight", "jfweight", "chargeable", "cw", "计费重量"
        ),
        "volume": pick("volume", "zdvolume", "total_volume", "cbm", "volume_m3", "体积"),
    }
    return master if any(value for value in master.values()) else None


def canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把模型可能使用的同义键名归一到标准结构（运输字段平铺 + 参与人嵌套）。"""

    lowered = {str(key).strip().lower(): value for key, value in payload.items()}
    canonical: dict[str, Any] = {}
    for key, aliases in FIELD_ALIASES.items():
        for candidate in (key, *aliases):
            value = lowered.get(candidate)
            if value not in (None, ""):
                canonical[key] = value
                break

    for party in PARTY_GROUPS:
        raw = lowered.get(party)
        nested: dict[str, Any] = {}
        if isinstance(raw, dict):
            nested = {str(key).lower(): value for key, value in raw.items()}
        elif isinstance(raw, str) and raw.strip():
            nested = {"name": raw}
        for name in PARTY_FIELD_NAMES:
            value = nested.get(name)
            if value in (None, ""):
                for alias in NESTED_PARTY_ALIASES.get(name, ()):
                    candidate = nested.get(alias)
                    if candidate not in (None, ""):
                        value = candidate
                        break
            if value in (None, ""):
                for alias in PARTY_ALIASES.get(f"{party}_{name}", ()):
                    candidate = lowered.get(alias)
                    if candidate not in (None, ""):
                        value = candidate
                        break
            if value not in (None, ""):
                canonical.setdefault(party, {})[name] = value
    return canonical


def _normalize_house_bill_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_fields = payload.get("fields")
    fields_source = raw_fields if isinstance(raw_fields, dict) else payload
    canonical = canonical_payload(fields_source)
    # 分单接口字段名与舱单口径不同：按映射表补齐接口键（hawb / mdg / zdpiece …）
    for compare_key, house_key in HOUSE_KEY_BY_COMPARE_KEY.items():
        if canonical.get(compare_key) not in (None, ""):
            canonical[house_key] = canonical[compare_key]
    fields = flatten_party_fields(canonical, keys=HOUSE_FIELD_KEYS, parties=PARTY_GROUPS)
    # 手机号只保留数字：去掉 + 号，中国号码再去掉 86 前缀（+8651257382098 → 51257382098）
    for party in PARTY_GROUPS:
        phone_key = PARTY_INTERFACE_KEYS[party]["phone"]
        if fields.get(phone_key):
            fields[phone_key] = normalize_phone(fields[phone_key]) or None
    warnings = _to_warnings(payload.get("warnings"))
    missing = [key for key in CORE_HOUSE_KEYS if not fields.get(key)]
    if missing:
        warnings.append(f"分单有 {len(missing)} 个核心字段未取到值")
    if not fields.get("hawb"):
        warnings.append("未识别到分单号，无法与舱单核对")
    return {
        "fields": fields,
        "confidence": _to_float(payload.get("confidence"), 0.6),
        "warnings": warnings,
    }


def _to_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, 0.0), 1.0)


def _to_int(value: Any, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_warnings(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []
