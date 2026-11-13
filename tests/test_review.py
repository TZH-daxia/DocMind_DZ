"""待审核清单：只来自候选层与整单置信度（一致性核对已取消）。"""

from app.schemas.documents import FieldCandidate, HouseBill
from app.schemas.fields import HOUSE_FIELD_KEYS, empty_field_map
from app.service.review import build_review_items


def house_fields(hawb: str = "HAWB0001", **overrides: str | None) -> dict[str, str | None]:
    values = empty_field_map(HOUSE_FIELD_KEYS)
    values.update(
        {
            "hawb": hawb,
            "sfg": "PVG",
            "mdg": "LAX",
            "hbh": "CA1234",
            "hbrq": "2026-10-20",
            "zdpiece": "12",
            "zdweight": "186.5",
            "zdvolume": "1.20",
        }
    )
    values.update(overrides)
    return values


def make_bill(confidence: float = 0.9, **overrides: str | None) -> HouseBill:
    return HouseBill(
        bill_id="bill_001",
        page_indexes=[2],
        fields=house_fields(**overrides),
        confidence=confidence,
    )


def test_field_evidence_goes_to_review_with_raw_text():
    """港口原文没有归一成三字码：候选层把原文带进待审核。"""

    bill = make_bill()
    bill.candidates = [
        FieldCandidate(
            field_key="sfg",
            value=None,
            raw_value="SHANGHAI",
            status="needs_review",
        )
    ]

    items = build_review_items([bill])

    entry = next(item for item in items if item.key == "sfg")
    assert entry.reason == "low_confidence"
    assert entry.evidence == "SHANGHAI"
    assert entry.tab_id == bill.bill_id


def test_low_confidence_creates_bill_level_item():
    bill = make_bill(confidence=0.4)

    items = build_review_items([bill], confidence_threshold=0.7)

    low = [item for item in items if item.reason == "low_confidence" and item.key == "_bill"]
    assert len(low) == 1


def test_conflict_candidate_goes_to_review():
    bill = make_bill()
    bill.candidates = [
        FieldCandidate(
            field_key="zdweight",
            value="190.0",
            status="conflict",
            alternatives=["186.5", "190.0"],
        )
    ]

    items = build_review_items([bill])

    entry = next(item for item in items if item.key == "zdweight")
    assert "多个不同值" in entry.message


def test_missing_candidate_does_not_go_to_review():
    """空值不是错误：没有 raw_value 的缺失候选不产生条目。"""

    bill = make_bill(confidence=0.95)
    bill.candidates = [
        FieldCandidate(field_key="pm", value=None, status="missing"),
    ]

    assert build_review_items([bill]) == []
