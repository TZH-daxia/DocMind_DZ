"""待审核清单。"""

from app.schemas.documents import HouseBill, ManifestDocument, ManifestEntry
from app.schemas.fields import empty_field_map
from app.service.consistency import compare
from app.service.review import build_review_items


def fields(hawb: str = "HAWB0001", **overrides: str | None) -> dict[str, str | None]:
    values = empty_field_map()
    values.update(
        {
            "hawb_no": hawb,
            "origin_port": "PVG",
            "dest_port": "LAX",
            "flight_no": "CA1234",
            "flight_date": "2026-10-20",
            "pieces": "12",
            "weight": "186.5",
            "volume": "1.20",
            "shipper_name": "SHANGHAI EXAMPLE CO., LTD",
            "shipper_address": "100 EXAMPLE ROAD, SHANGHAI, CHINA",
            "shipper_phone": "+86 21 5888 0000",
            "shipper_email": "shipper@example.com",
            "consignee_name": "EXAMPLE TRADING INC.",
            "consignee_address": "1000 EXAMPLE BLVD, LOS ANGELES, CA 90001",
            "consignee_phone": "+1 213 555 0100",
            "consignee_email": "consignee@example.com",
        }
    )
    values.update(overrides)
    return values


def make_bill(confidence: float = 0.9, **overrides: str | None) -> HouseBill:
    return HouseBill(
        bill_id="bill_001",
        segment_id="seg_002",
        page_indexes=[2],
        fields=fields(**overrides),
        confidence=confidence,
    )


def make_manifest(**overrides: str | None) -> ManifestDocument:
    return ManifestDocument(
        segment_id="seg_004",
        entries=[
            ManifestEntry(
                entry_id="mft_0001",
                line_no=1,
                fields=fields("HAWB0001", **overrides),
            )
        ],
    )


def test_mismatch_and_missing_go_to_review():
    bill = make_bill(weight="190.0")
    manifest = make_manifest(weight="220.0", shipper_email=None)
    comparisons, _, _ = compare([bill.model_dump()], manifest)

    items = build_review_items([bill], comparisons, manifest)

    assert any(item.reason == "mismatch" and item.key == "weight" for item in items)
    assert any(item.reason == "missing" and item.key == "shipper_email" for item in items)
    # 字段顺序：重量在发货人邮箱之前
    keys = [item.key for item in items]
    assert keys.index("weight") < keys.index("shipper_email")


def test_low_confidence_creates_bill_level_item():
    bill = make_bill(confidence=0.4)
    manifest = make_manifest()
    comparisons, _, _ = compare([bill.model_dump()], manifest)

    items = build_review_items([bill], comparisons, manifest, confidence_threshold=0.7)

    low = [item for item in items if item.reason == "low_confidence"]
    assert len(low) == 1
    assert low[0].key == "_bill"


def test_unmatched_manifest_row_goes_to_review():
    bill = make_bill(hawb_no="HAWB9999")
    manifest = make_manifest()
    comparisons, _, _ = compare([bill.model_dump()], manifest)

    items = build_review_items([bill], comparisons, manifest)

    assert any(item.tab_id == "manifest" and item.reason == "missing" for item in items)
    assert any("舱单中未找到" in item.message for item in items)
