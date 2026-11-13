"""待审核清单：把候选层的不确定项整理成前端可以直接列出的条目。

一致性核对已取消，待审核只来自两处：

- **字段级**：港口原文没能归一成三字码、同字段多值冲突、置信度低、引用对不上；
- **整单级**：整份分单的抽取置信度低于阈值。

一方为空的字段不进入待审核（既不是冲突也不是错误）。
"""

from __future__ import annotations

from app.schemas.documents import HouseBill, ReviewItem
from app.schemas.fields import FIELD_LABELS, HOUSE_KEY_BY_COMPARE_KEY


def build_review_items(
    house_bills: list[HouseBill],
    confidence_threshold: float = 0.7,
) -> list[ReviewItem]:
    """按「分单顺序 → 候选顺序」生成待审核条目。"""

    items: list[ReviewItem] = []

    for bill in house_bills:
        hawb_no = bill.fields.get(HOUSE_KEY_BY_COMPARE_KEY["hawb_no"])

        for candidate in bill.candidates:
            message = ""
            # 港口等无法确定标准值的字段：候选层保留了归一前原文，进待审核
            if (
                candidate.field_key in {"sfg", "mdg"}
                and not candidate.value
                and candidate.raw_value
            ):
                message = "原文未明确港口三字码，请人工确认"
            elif candidate.value:
                if candidate.status == "conflict":
                    others = "、".join(candidate.alternatives) or "多个值"
                    message = f"页面存在多个不同值（{others}），请确认"
                elif candidate.status == "needs_review":
                    message = (
                        candidate.warnings[0]
                        if candidate.warnings
                        else "字段置信度较低或原文引用对不上，请确认"
                    )
                elif candidate.confidence < confidence_threshold:
                    message = f"字段置信度仅 {candidate.confidence:.0%}，请确认"
            if not message:
                continue

            quote = candidate.evidence[0].quote if candidate.evidence else ""
            items.append(
                ReviewItem(
                    tab_id=bill.bill_id,
                    bill_id=bill.bill_id,
                    hawb_no=hawb_no,
                    key=candidate.field_key,
                    label=FIELD_LABELS.get(candidate.field_key, candidate.field_key),
                    reason="low_confidence",
                    message=message,
                    # 优先展示真实原文片段；没有引用时（如港口未归一）才退回原文值
                    evidence=quote or candidate.raw_value or "",
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
