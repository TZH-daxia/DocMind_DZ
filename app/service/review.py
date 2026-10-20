"""待审核清单：把核对结果整理成前端可以直接列出的条目。

口径：
- `mismatch`：分单与舱单值冲突（必须人工确认）；
- `missing`：一方缺失，或舱单里找不到该分单号；
- `similar`：文本近似，需要人眼确认；
- `low_confidence`：整单抽取置信度低于阈值。

顺序保持「分单顺序 → 字段顺序」，舱单未匹配行排在最后，方便前端按页签分组。
"""

from __future__ import annotations

from app.schemas.documents import (
    HouseBill,
    HouseBillComparison,
    ManifestDocument,
    ReviewItem,
)


def build_review_items(
    house_bills: list[HouseBill],
    comparisons: list[HouseBillComparison],
    manifest: ManifestDocument | None,
    confidence_threshold: float = 0.7,
) -> list[ReviewItem]:
    comparisons_by_bill = {item.bill_id: item for item in comparisons}
    items: list[ReviewItem] = []

    for bill in house_bills:
        hawb_no = bill.fields.get("hawb_no")
        comparison = comparisons_by_bill.get(bill.bill_id)
        if comparison is None:
            items.append(
                ReviewItem(
                    tab_id=bill.bill_id,
                    bill_id=bill.bill_id,
                    hawb_no=hawb_no,
                    key="hawb_no",
                    label="分单号",
                    reason="missing",
                    message="该分单未生成核对结果",
                )
            )
            continue

        for field in comparison.fields:
            if field.status == "mismatch":
                items.append(
                    ReviewItem(
                        tab_id=bill.bill_id,
                        bill_id=bill.bill_id,
                        hawb_no=hawb_no,
                        key=field.key,
                        label=field.label,
                        reason="mismatch",
                        message=(
                            f"分单「{field.house_value or '空'}」"
                            f" ≠ 舱单「{field.manifest_value or '空'}」"
                        ),
                    )
                )
            elif field.status in {"missing_in_manifest", "missing_in_house_bill"}:
                items.append(
                    ReviewItem(
                        tab_id=bill.bill_id,
                        bill_id=bill.bill_id,
                        hawb_no=hawb_no,
                        key=field.key,
                        label=field.label,
                        reason="missing",
                        message=(
                            "舱单中该字段为空"
                            if field.status == "missing_in_manifest"
                            else "分单中该字段为空"
                        ),
                    )
                )
            elif field.status == "similar":
                items.append(
                    ReviewItem(
                        tab_id=bill.bill_id,
                        bill_id=bill.bill_id,
                        hawb_no=hawb_no,
                        key=field.key,
                        label=field.label,
                        reason="similar",
                        message=f"文本近似（{field.similarity:.0%}），请确认",
                    )
                )

        if comparison.manifest_entry_id is None:
            items.append(
                ReviewItem(
                    tab_id=bill.bill_id,
                    bill_id=bill.bill_id,
                    hawb_no=hawb_no,
                    key="hawb_no",
                    label="分单号",
                    reason="missing",
                    message="舱单中未找到该分单号",
                )
            )

        if bill.confidence < confidence_threshold:
            items.append(
                ReviewItem(
                    tab_id=bill.bill_id,
                    bill_id=bill.bill_id,
                    hawb_no=hawb_no,
                    key="_bill",
                    label="整单",
                    reason="low_confidence",
                    message=f"抽取置信度仅 {bill.confidence:.0%}，请核对整单",
                )
            )

    matched_entries = {
        item.manifest_entry_id for item in comparisons if item.manifest_entry_id is not None
    }
    if manifest is not None:
        for entry in manifest.entries:
            if entry.entry_id in matched_entries:
                continue
            items.append(
                ReviewItem(
                    tab_id="manifest",
                    key="hawb_no",
                    label="分单号",
                    reason="missing",
                    message=f"舱单第 {entry.line_no} 行未匹配到分单",
                )
            )

    return _dedupe(items)


def _dedupe(items: list[ReviewItem]) -> list[ReviewItem]:
    seen: set[tuple[str, str, str, str]] = set()
    result: list[ReviewItem] = []
    for item in items:
        key = (item.tab_id, item.key, item.reason, item.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
