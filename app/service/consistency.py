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
)
from app.schemas.fields import COMPARE_FIELD_SPECS, FieldSpec
from app.service.normalizers import (
    normalize_date,
    normalize_doc_no,
    normalize_email,
    normalize_flight_no,
    normalize_int,
    normalize_number,
    normalize_phone,
    normalize_port,
    similarity,
    similarity_address,
)

SIMILAR_TEXT_THRESHOLD = 0.92
NUMBER_RELATIVE_TOLERANCE = 0.02
NUMBER_ABSOLUTE_TOLERANCE = 0.01


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
        house_port = normalize_port(house_value)
        manifest_port = normalize_port(manifest_value)
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
    if any(field.status == "mismatch" for field in fields):
        return "inconsistent"
    if any(
        field.status.startswith("missing") and field.status != "missing_both"
        for field in fields
    ):
        return "incomplete"
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
        hawb_key = normalize_doc_no(fields.get("hawb_no"))
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
                    hawb_no=fields.get("hawb_no"),
                    status="incomplete",
                    missing_count=len(COMPARE_FIELD_SPECS),
                    fields=_missing_comparisons(),
                )
            )
            if hawb_key:
                warnings.append(
                    f"分单 {fields.get('hawb_no')} 在舱单中未找到对应行"
                )
            continue

        entry = entries[entry_position]
        field_results = [
            compare_field(spec, fields.get(spec.key), entry.fields.get(spec.key))
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
                hawb_no=fields.get("hawb_no") or entry.fields.get("hawb_no"),
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

    duplicate_bills = _duplicates(
        normalize_doc_no(bill.get("fields", {}).get("hawb_no")) for bill in house_bills
    )
    if duplicate_bills:
        warnings.append("分单号重复：" + "、".join(duplicate_bills))
    duplicate_entries = _duplicates(
        normalize_doc_no(entry.fields.get("hawb_no")) for entry in entries
    )
    if duplicate_entries:
        warnings.append("舱单分单号重复：" + "、".join(duplicate_entries))

    return comparisons, summary, warnings


def _duplicates(values) -> list[str]:
    seen: dict[str, int] = defaultdict(int)
    for value in values:
        if value:
            seen[value] += 1
    return [value for value, count in seen.items() if count > 1]
