"""待审核清单：把核对结果整理成前端可以直接列出的条目。

口径：
- `mismatch`：分单与舱单值冲突（必须人工确认），保持原有差异展示；
- `similar`：文本近似（识别不确定），字段下附带「原文：…」供人眼确认；
- `low_confidence`：整单抽取置信度低于阈值，附带原文片段；
- 一方为空（`missing`）不再进入待审核，也不提示；舱单行没有对应分单同样不提示。

顺序保持「分单顺序 → 字段顺序」，舱单未匹配行排在最后，方便前端按页签分组。
"""

from __future__ import annotations

from app.schemas.documents import (
    HouseBill,
    HouseBillComparison,
    ManifestDocument,
    ReviewItem,
    TotalsCheck,
)
from app.schemas.fields import FIELD_LABELS, HOUSE_KEY_BY_COMPARE_KEY


def build_review_items(
    house_bills: list[HouseBill],
    comparisons: list[HouseBillComparison],
    manifest: ManifestDocument | None,
    confidence_threshold: float = 0.7,
    totals_checks: list[TotalsCheck] | None = None,
) -> list[ReviewItem]:
    comparisons_by_bill = {item.bill_id: item for item in comparisons}
    items: list[ReviewItem] = []

    for bill in house_bills:
        hawb_no = bill.fields.get(HOUSE_KEY_BY_COMPARE_KEY["hawb_no"])
        # 港口等无法确定标准值的字段：进待审核，并带上原文供人工确认
        for key, raw in (bill.field_evidence or {}).items():
            items.append(
                ReviewItem(
                    tab_id=bill.bill_id,
                    bill_id=bill.bill_id,
                    hawb_no=hawb_no,
                    key=key,
                    label=FIELD_LABELS.get(key, key),
                    reason="low_confidence",
                    message="原文未明确港口三字码，请人工确认",
                    evidence=raw,
                )
            )
        comparison = comparisons_by_bill.get(bill.bill_id)
        if comparison is None:
            items.append(
                ReviewItem(
                    tab_id=bill.bill_id,
                    bill_id=bill.bill_id,
                    hawb_no=hawb_no,
                    key=HOUSE_KEY_BY_COMPARE_KEY["hawb_no"],
                    label="分单号",
                    reason="low_confidence",
                    message="该分单未生成核对结果，请人工确认",
                )
            )
            continue

        # 分单在舱单中找不到对应行：整单不进入待审核（避免 38 个字段级缺失刷屏）
        if comparison.manifest_entry_id is None:
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
            elif field.status == "similar":
                # 文本近似 = 识别不确定：进待审核，并在字段下引用原文
                items.append(
                    ReviewItem(
                        tab_id=bill.bill_id,
                        bill_id=bill.bill_id,
                        hawb_no=hawb_no,
                        key=field.key,
                        label=field.label,
                        reason="similar",
                        message=f"文本近似（{field.similarity:.0%}），请确认",
                        evidence=field.house_value or "",
                    )
                )

        if bill.confidence < confidence_threshold:
            evidence = " ".join((bill.source_excerpt or "").split())[:80]
            items.append(
                ReviewItem(
                    tab_id=bill.bill_id,
                    bill_id=bill.bill_id,
                    hawb_no=hawb_no,
                    key="_bill",
                    label="整单",
                    reason="low_confidence",
                    message=f"抽取置信度仅 {bill.confidence:.0%}，请核对整单",
                    evidence=evidence,
                )
            )

    # 汇总核对（分单合计 vs 舱单总运单）：只保留不一致，缺值不再提示
    for check in totals_checks or []:
        if check.status != "mismatch":
            continue
        items.append(
            ReviewItem(
                tab_id="manifest",
                key=f"totals_{check.key}",
                label=check.label,
                reason="mismatch",
                message=check.note or f"汇总{check.label}不一致",
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
