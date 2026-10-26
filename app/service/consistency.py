"""分单 ↔ 舱单一致性核对。

核对主键：分单号（归一化后精确匹配）。
- 匹配上的：逐字段比较分单与舱单的同名字段，输出 match / similar / mismatch / 缺失；
- 只有分单没有舱单行：`manifest_entry_id=None`，状态 incomplete；
- 只有舱单行没有分单：计入 summary.manifest_only。

判定口径：
- 数值字段（件数/重量/体积）按数值比较，重量、体积允许 2% 相对误差；
- 港口先归一化成三字码再比；
- 航班号忽略空格/连字符，日期转 ISO；
- 名称、地址先去掉空白与标点后比较，相似度 ≥0.92 记为 similar（人工复核但不判不一致）；
- 电话只比数字，邮箱小写比较。
"""

from __future__ import annotations

from collections import defaultdict

from app.schemas.documents import (
    BillStatus,
    ComparisonSummary,
    FieldComparison,
    HouseBillComparison,
    ManifestDocument,
    TotalsCheck,
)
from app.schemas.fields import COMPARE_FIELD_SPECS, HOUSE_KEY_BY_COMPARE_KEY, FieldSpec
from app.service.normalizers import (
    normalize_date,
    normalize_doc_no,
    normalize_email,
    normalize_flight_no,
    normalize_int,
    normalize_number,
    normalize_phone,
    similarity,
    similarity_address,
)
from app.service.ports import resolve_port

SIMILAR_TEXT_THRESHOLD = 0.92
NUMBER_RELATIVE_TOLERANCE = 0.02
NUMBER_ABSOLUTE_TOLERANCE = 0.01


def _normalize_port_value(value: str | None) -> str:
    """港口归一：优先缓存表三字码，无法确定时保留原文，供相似度比较。"""

    code, raw = resolve_port(value)
    return code or raw


def _is_blank(value: str | None) -> bool:
    return value is None or not str(value).strip()


def _numeric_match(
    house_value: float | None, manifest_value: float | None, *, tolerance: float
) -> bool:
    if house_value is None or manifest_value is None:
        return False
    if house_value == manifest_value:
        return True
    difference = abs(house_value - manifest_value)
    if difference <= NUMBER_ABSOLUTE_TOLERANCE:
        return True
    base = max(abs(house_value), abs(manifest_value), 1e-9)
    return difference / base <= tolerance


def compare_field(
    spec: FieldSpec, house_value: str | None, manifest_value: str | None
) -> FieldComparison:
    if _is_blank(house_value) and _is_blank(manifest_value):
        return FieldComparison(
            key=spec.key,
            label=spec.label,
            status="missing_both",
            similarity=1.0,
            note="双方均无值",
        )
    if _is_blank(house_value):
        return FieldComparison(
            key=spec.key,
            label=spec.label,
            house_value=house_value,
            manifest_value=manifest_value,
            status="missing_in_house_bill",
            similarity=0.0,
            note="分单缺失该字段",
        )
    if _is_blank(manifest_value):
        return FieldComparison(
            key=spec.key,
            label=spec.label,
            house_value=house_value,
            manifest_value=manifest_value,
            status="missing_in_manifest",
            similarity=0.0,
            note="舱单缺失该字段",
        )

    status: str = "match"
    score = 1.0
    note = ""

    if spec.kind == "integer":
        house_number = normalize_int(house_value)
        manifest_number = normalize_int(manifest_value)
        if _numeric_match(house_number, manifest_number, tolerance=0.0):
            note = "数值一致"
        elif house_number is None or manifest_number is None:
            status, score = "mismatch", 0.0
            note = "非数值，无法比较"
        else:
            status, score = "mismatch", similarity(house_value, manifest_value)
            note = f"数值不一致：{house_number} ≠ {manifest_number}"
    elif spec.kind == "number":
        house_number = normalize_number(house_value)
        manifest_number = normalize_number(manifest_value)
        if _numeric_match(house_number, manifest_number, tolerance=NUMBER_RELATIVE_TOLERANCE):
            note = "数值在允许误差内一致"
        elif house_number is None or manifest_number is None:
            status, score = "mismatch", 0.0
            note = "非数值，无法比较"
        else:
            status, score = "mismatch", 0.0
            note = f"数值不一致：{house_number} ≠ {manifest_number}"
    elif spec.kind == "port":
        house_port = _normalize_port_value(house_value)
        manifest_port = _normalize_port_value(manifest_value)
        if house_port == manifest_port:
            note = "港口归一化后一致" if house_port else "一致"
        else:
            score = similarity(house_port, manifest_port)
            status = "similar" if score >= SIMILAR_TEXT_THRESHOLD else "mismatch"
            note = f"港口不一致：{house_value} ≠ {manifest_value}"
    elif spec.kind == "flight":
        house_flight = normalize_flight_no(house_value)
        manifest_flight = normalize_flight_no(manifest_value)
        if house_flight == manifest_flight:
            note = "航班号归一化后一致"
        else:
            status, score = "mismatch", 0.0
            note = f"航班号不一致：{house_value} ≠ {manifest_value}"
    elif spec.kind == "date":
        house_date = normalize_date(house_value)
        manifest_date = normalize_date(manifest_value)
        if house_date == manifest_date:
            note = "日期归一化后一致"
        else:
            status, score = "mismatch", 0.0
            note = f"日期不一致：{house_value} ≠ {manifest_value}"
    elif spec.kind == "phone":
        house_phone = normalize_phone(house_value)
        manifest_phone = normalize_phone(manifest_value)
        if house_phone == manifest_phone:
            note = "电话数字一致"
        elif not house_phone or not manifest_phone:
            status, score = "mismatch", 0.0
            note = "电话无法归一化比较"
        else:
            status, score = "mismatch", 0.0
            note = f"电话不一致：{house_value} ≠ {manifest_value}"
    elif spec.kind == "email":
        house_email = normalize_email(house_value)
        manifest_email = normalize_email(manifest_value)
        if house_email == manifest_email:
            note = "邮箱一致"
        else:
            status, score = "mismatch", 0.0
            note = f"邮箱不一致：{house_value} ≠ {manifest_value}"
    else:
        if spec.key.endswith("_address"):
            score = similarity_address(house_value, manifest_value)
        else:
            score = similarity(house_value, manifest_value)
        if score >= 0.999:
            note = "一致"
        elif score >= SIMILAR_TEXT_THRESHOLD:
            status = "similar"
            note = f"文本近似（相似度 {score:.0%}）"
        else:
            status = "mismatch"
            note = f"文本不一致（相似度 {score:.0%}）"
        if spec.key == "hawb_no":
            house_doc_no = normalize_doc_no(house_value)
            manifest_doc_no = normalize_doc_no(manifest_value)
            if house_doc_no == manifest_doc_no:
                status, score, note = "match", 1.0, "一致"

    return FieldComparison(
        key=spec.key,
        label=spec.label,
        house_value=house_value,
        manifest_value=manifest_value,
        status=status,  # type: ignore[arg-type]
        similarity=round(score, 4),
        note=note,
    )


def _bill_status(fields: list[FieldComparison]) -> BillStatus:
    """整单状态只看真实差异：缺值不算「信息不全」，只在舱单字段上标「未校验」。"""

    if any(field.status == "mismatch" for field in fields):
        return "inconsistent"
    return "consistent"


def _missing_comparisons() -> list[FieldComparison]:
    return [
        FieldComparison(
            key=spec.key,
            label=spec.label,
            manifest_value=None,
            status="missing_in_manifest",
            similarity=0.0,
            note="舱单中未找到该分单号",
        )
        for spec in COMPARE_FIELD_SPECS
    ]


def compare(
    house_bills: list[dict],
    manifest: ManifestDocument | None,
) -> tuple[list[HouseBillComparison], ComparisonSummary, list[str]]:
    """逐份分单与舱单核对。

    Args:
        house_bills: HouseBill 的 dump 列表（含 fields/bill_id）。
        manifest: 舱单文档；None 表示未识别到舱单。
    """

    warnings: list[str] = []
    entries = manifest.entries if manifest else []

    entry_index: dict[str, list[int]] = defaultdict(list)
    for position, entry in enumerate(entries):
        key = normalize_doc_no(entry.fields.get("hawb_no"))
        if key:
            entry_index[key].append(position)

    used: set[int] = set()
    comparisons: list[HouseBillComparison] = []

    for bill in house_bills:
        fields = bill.get("fields") or {}
        house_hawb = fields.get(HOUSE_KEY_BY_COMPARE_KEY["hawb_no"])
        hawb_key = normalize_doc_no(house_hawb)
        entry_position = None
        if hawb_key:
            for candidate in entry_index.get(hawb_key, []):
                if candidate not in used:
                    entry_position = candidate
                    used.add(candidate)
                    break

        if entry_position is None:
            comparisons.append(
                HouseBillComparison(
                    bill_id=str(bill.get("bill_id", "")),
                    hawb_no=house_hawb,
                    status="incomplete",
                    missing_count=len(COMPARE_FIELD_SPECS),
                    fields=_missing_comparisons(),
                )
            )
            if hawb_key:
                warnings.append(f"分单 {house_hawb} 在舱单中未找到对应行")
            continue

        entry = entries[entry_position]
        field_results = [
            compare_field(
                spec,
                # 分单字段已按单证接口命名，个别字段与舱单口径不同名
                fields.get(HOUSE_KEY_BY_COMPARE_KEY.get(spec.key, spec.key)),
                entry.fields.get(spec.key),
            )
            for spec in COMPARE_FIELD_SPECS
        ]
        mismatch_count = sum(1 for field in field_results if field.status == "mismatch")
        missing_count = sum(
            1
            for field in field_results
            if field.status in {"missing_in_house_bill", "missing_in_manifest"}
        )
        comparisons.append(
            HouseBillComparison(
                bill_id=str(bill.get("bill_id", "")),
                hawb_no=house_hawb or entry.fields.get("hawb_no"),
                manifest_entry_id=entry.entry_id,
                manifest_line_no=entry.line_no,
                status=_bill_status(field_results),
                match_count=sum(
                    1 for field in field_results if field.status in {"match", "similar"}
                ),
                mismatch_count=mismatch_count,
                missing_count=missing_count,
                fields=field_results,
            )
        )

    manifest_only = len(entries) - len(used)
    summary = ComparisonSummary(
        total_house_bills=len(house_bills),
        total_manifest_entries=len(entries),
        consistent=sum(1 for item in comparisons if item.status == "consistent"),
        inconsistent=sum(1 for item in comparisons if item.status == "inconsistent"),
        incomplete=sum(1 for item in comparisons if item.status == "incomplete"),
        house_only=sum(1 for item in comparisons if item.manifest_entry_id is None),
        manifest_only=max(manifest_only, 0),
        field_mismatch_count=sum(item.mismatch_count for item in comparisons),
    )

    if manifest_only > 0:
        unmatched = [
            entry.fields.get("hawb_no")
            for position, entry in enumerate(entries)
            if position not in used
        ]
        warnings.append(
            "舱单中存在未匹配到分单的行：" + "、".join(str(item) for item in unmatched if item)
        )

    house_hawb_key = HOUSE_KEY_BY_COMPARE_KEY["hawb_no"]
    duplicate_bills = _duplicates(
        normalize_doc_no(bill.get("fields", {}).get(house_hawb_key)) for bill in house_bills
    )
    if duplicate_bills:
        warnings.append("分单号重复：" + "、".join(duplicate_bills))
    duplicate_entries = _duplicates(
        normalize_doc_no(entry.fields.get("hawb_no")) for entry in entries
    )
    if duplicate_entries:
        warnings.append("舱单分单号重复：" + "、".join(duplicate_entries))

    return comparisons, summary, warnings


TOTALS_LABELS: dict[str, str] = {
    "dest_port": "目的港",
    "pieces": "件数",
    "weight": "重量",
    "chargeable_weight": "计费重量",
    "volume": "体积",
}


def _sum_field(
    house_bills: list[dict], key: str, kind: str
) -> tuple[float, int]:
    """把多份分单的某个数值字段求和，返回 (合计, 有效份数)。"""

    total = 0.0
    found = 0
    house_key = HOUSE_KEY_BY_COMPARE_KEY.get(key, key)
    for bill in house_bills:
        value = (bill.get("fields") or {}).get(house_key)
        number = normalize_int(value) if kind == "integer" else normalize_number(value)
        if number is None:
            continue
        total += number
        found += 1
    return round(total, 4), found


def _format_total(value: float | None, kind: str) -> str | None:
    if value is None:
        return None
    if kind == "integer":
        return str(int(round(value)))
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _totals_check(
    key: str,
    house_bills: list[dict],
    manifest_value: str | None,
    *,
    kind: str = "number",
    mode: str = "equal",
) -> TotalsCheck:
    label = TOTALS_LABELS[key]
    total, found = _sum_field(house_bills, key, kind)
    master_number = (
        normalize_int(manifest_value)
        if kind == "integer"
        else normalize_number(manifest_value)
    )
    house_text = _format_total(total, kind) if found else None
    master_text = _format_total(master_number, kind) if master_number is not None else None

    if master_number is None:
        return TotalsCheck(
            key=key,
            label=label,
            status="missing",
            house_total=house_text,
            manifest_total=None,
            note=f"舱单未提供总运单{label}",
        )
    if not found:
        return TotalsCheck(
            key=key,
            label=label,
            status="missing",
            house_total=None,
            manifest_total=master_text,
            note=f"分单未提供{label}",
        )

    if mode == "at_least":
        ok = total + NUMBER_ABSOLUTE_TOLERANCE >= master_number
        note = (
            "分单合计不小于总运单"
            if ok
            else f"分单合计 {house_text} 小于总运单 {master_text}"
        )
    else:
        ok = _numeric_match(total, master_number, tolerance=NUMBER_RELATIVE_TOLERANCE)
        note = (
            "分单合计与总运单一致"
            if ok
            else f"分单合计 {house_text} ≠ 总运单 {master_text}"
        )
    return TotalsCheck(
        key=key,
        label=label,
        status="match" if ok else "mismatch",
        house_total=house_text,
        manifest_total=master_text,
        note=note,
    )


def compare_totals(
    house_bills: list[dict],
    manifest: ManifestDocument | None,
) -> list[TotalsCheck]:
    """分单合计与舱单总运单汇总的核对（5 条汇总规则）。"""

    if manifest is None:
        return []
    master = manifest.master
    checks: list[TotalsCheck] = []

    # 1. 目的港：任意一份分单的目的港与总运单不一致即判失败
    house_port_key = HOUSE_KEY_BY_COMPARE_KEY["dest_port"]
    house_ports = [
        _normalize_port_value((bill.get("fields") or {}).get(house_port_key))
        for bill in house_bills
    ]
    house_ports = [port for port in house_ports if port]
    master_port = _normalize_port_value(master.dest_port) if master else ""
    if not master_port:
        checks.append(
            TotalsCheck(
                key="dest_port",
                label="目的港",
                status="missing",
                house_total="、".join(sorted(set(house_ports))) or None,
                manifest_total=None,
                note="舱单未提供总运单目的港",
            )
        )
    elif not house_ports:
        checks.append(
            TotalsCheck(
                key="dest_port",
                label="目的港",
                status="missing",
                house_total=None,
                manifest_total=master_port,
                note="分单未提供目的港",
            )
        )
    else:
        mismatched = sorted({port for port in house_ports if port != master_port})
        if mismatched:
            checks.append(
                TotalsCheck(
                    key="dest_port",
                    label="目的港",
                    status="mismatch",
                    house_total="、".join(sorted(set(house_ports))),
                    manifest_total=master_port,
                    note=f"分单目的港（{'、'.join(mismatched)}）与舱单总运单不一致",
                )
            )
        else:
            checks.append(
                TotalsCheck(
                    key="dest_port",
                    label="目的港",
                    status="match",
                    house_total=master_port,
                    manifest_total=master_port,
                    note="全部分单目的港与总运单一致",
                )
            )

    # 2-5. 件数 / 重量 / 计费重量 / 体积
    checks.append(
        _totals_check(
            "pieces", house_bills, master.pieces if master else None, kind="integer"
        )
    )
    checks.append(
        _totals_check("weight", house_bills, master.weight if master else None)
    )
    checks.append(
        _totals_check(
            "chargeable_weight",
            house_bills,
            master.chargeable_weight if master else None,
            mode="at_least",
        )
    )
    checks.append(
        _totals_check("volume", house_bills, master.volume if master else None)
    )
    return checks


def _duplicates(values) -> list[str]:
    seen: dict[str, int] = defaultdict(int)
    for value in values:
        if value:
            seen[value] += 1
    return [value for value, count in seen.items() if count > 1]
