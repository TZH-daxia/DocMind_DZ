"""候选层：原文引用校验与待审核衔接。"""

from app.schemas.documents import (
    FieldCandidate,
    FieldEvidence,
    HouseBill,
    PageRecord,
)
from app.service.pipeline import _validate_candidates
from app.service.review import build_review_items


def test_candidate_quote_is_checked_against_transcription():
    pages = [PageRecord(index=1, text="HOUSE AIR WAYBILL\nHAWB: HAWB0001")]
    candidates = [
        {
            "field_key": "hawb",
            "value": "HAWB0001",
            "status": "confirmed",
            "confidence": 0.9,
            "evidence": [{"quote": "HAWB: HAWB0001"}],
            "warnings": [],
        }
    ]

    updated, warnings = _validate_candidates(pages, [1], candidates)

    assert updated[0]["status"] == "confirmed"
    assert warnings == []

    updated[0]["evidence"][0]["quote"] = "完全不存在的一段引用"
    updated, warnings = _validate_candidates(pages, [1], updated)

    assert updated[0]["status"] == "needs_review"
    assert "原文引用" in warnings[0]


def test_candidate_without_any_quote_is_not_downgraded():
    """整单一条 quote 都没有（旧格式/模型漏输出）时不逐字段降级。"""

    pages = [PageRecord(index=1, text="HOUSE AIR WAYBILL")]
    candidates = [
        {
            "field_key": "hawb",
            "value": "HAWB0001",
            "status": "confirmed",
            "confidence": 0.9,
            "evidence": [],
            "warnings": [],
        }
    ]

    updated, warnings = _validate_candidates(pages, [1], candidates)

    assert updated[0]["status"] == "confirmed"
    assert "原文引用" in warnings[0]


def test_candidate_needs_review_enters_review_list():
    bill = HouseBill(
        bill_id="bill_001",
        segment_id="seg_001",
        page_indexes=[1],
        fields={"hawb": "HAWB0001", "pm": "GOODS"},
        candidates=[
            FieldCandidate(
                field_key="pm",
                value="GOODS",
                status="needs_review",
                confidence=0.9,
                evidence=[FieldEvidence(quote="NATURE AND QUANTITY GOODS")],
                warnings=["原文引用未在转写文本中找到"],
            )
        ],
    )

    items = build_review_items([bill], [], None)
    target = [item for item in items if item.key == "pm"]

    assert target
    assert target[0].reason == "low_confidence"
    assert "原文引用" in target[0].message
    assert target[0].evidence == "NATURE AND QUANTITY GOODS"
