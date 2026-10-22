"""待审核清单。"""

from app.schemas.documents import HouseBill, ManifestDocument, ManifestEntry
from app.schemas.fields import HOUSE_FIELD_KEYS, MANIFEST_FIELD_KEYS, empty_field_map
from app.service.consistency import compare
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
            "companytitle_fhr_hawb": "SHANGHAI EXAMPLE CO., LTD",
            "address_fhr_hawb": "100 EXAMPLE ROAD, SHANGHAI, CHINA",
            "phone_fhr_hawb": "+86 21 5888 0000",
            "email_fhr_hawb": "shipper@example.com",
            "companytitle_shr_hawb": "EXAMPLE TRADING INC.",
            "address_shr_hawb": "1000 EXAMPLE BLVD, LOS ANGELES, CA 90001",
            "phone_shr_hawb": "+1 213 555 0100",
            "email_shr_hawb": "consignee@example.com",
        }
    )
    values.update(overrides)
    return values


def manifest_fields(hawb: str = "HAWB0001", **overrides: str | None) -> dict[str, str | None]:
    values = empty_field_map(MANIFEST_FIELD_KEYS)
    values.update(
        {
            "hawb_no": hawb,
            "dest_port": "LAX",
            "pieces": "12",
            "weight": "186.5",
            "volume": "1.20",
        }
    )
    values.update(overrides)
    return values


def make_bill(confidence: float = 0.9, **overrides: str | None) -> HouseBill:
    return HouseBill(
        bill_id="bill_001",
        segment_id="seg_002",
        page_indexes=[2],
        fields=house_fields(**overrides),
        confidence=confidence,
    )


def make_manifest(**overrides: str | None) -> ManifestDocument:
    return ManifestDocument(
        segment_id="seg_004",
        entries=[
            ManifestEntry(
                entry_id="mft_0001",
                line_no=1,
                fields=manifest_fields("HAWB0001", **overrides),
            )
        ],
    )


def test_mismatch_goes_to_review_but_missing_does_not():
    bill = make_bill(zdweight="190.0")
    manifest = make_manifest(weight="220.0", pieces=None)
    comparisons, _, _ = compare([bill.model_dump()], manifest)

    items = build_review_items([bill], comparisons, manifest)

    assert any(item.reason == "mismatch" and item.key == "weight" for item in items)
    # 空值不再进入待审核
    assert not any(item.reason == "missing" for item in items)
    assert not any(item.key == "pieces" for item in items)


def test_field_evidence_goes_to_review_with_raw_text():
    bill = make_bill()
    bill.field_evidence = {"sfg": "SHANGHAI"}

    items = build_review_items([bill], [], None)

    entry = next(item for item in items if item.key == "sfg")
    assert entry.reason == "low_confidence"
    assert entry.evidence == "SHANGHAI"
    assert entry.tab_id == bill.bill_id


def test_low_confidence_creates_bill_level_item():
    bill = make_bill(confidence=0.4)
    manifest = make_manifest()
    comparisons, _, _ = compare([bill.model_dump()], manifest)

    items = build_review_items([bill], comparisons, manifest, confidence_threshold=0.7)

    low = [item for item in items if item.reason == "low_confidence"]
    assert len(low) == 1
    assert low[0].key == "_bill"


def test_unmatched_rows_do_not_go_to_review():
    bill = make_bill(hawb_no="HAWB9999")
    manifest = make_manifest()
    comparisons, _, _ = compare([bill.model_dump()], manifest)

    items = build_review_items([bill], comparisons, manifest)

    # 分单/舱单任一方没有对应行都属于"缺失"，不再进待审核
    assert items == []
