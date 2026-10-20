"""分单 ↔ 舱单核对。"""

from app.schemas.documents import ManifestDocument, ManifestEntry
from app.schemas.fields import empty_field_map
from app.service.consistency import compare


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
            "shipper_name": "SHANGHAI EXAMPLE TRADING CO., LTD",
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


def bill(hawb: str = "HAWB0001", **overrides: str | None) -> dict:
    return {"bill_id": f"bill_{hawb}", "fields": fields(hawb, **overrides)}


def manifest(rows: list[dict]) -> ManifestDocument:
    entries = [
        ManifestEntry(
            entry_id=f"mft_{index}",
            line_no=index,
            fields=fields(row.pop("hawb_no"), **row),
        )
        for index, row in enumerate(rows, start=1)
    ]
    return ManifestDocument(segment_id="seg_manifest", entries=entries)


def test_identical_bill_is_consistent():
    comparisons, summary, warnings = compare([bill()], manifest([{"hawb_no": "HAWB0001"}]))
    assert comparisons[0].status == "consistent"
    assert summary.consistent == 1
    assert summary.field_mismatch_count == 0
    assert warnings == []


def test_hawb_match_ignores_dashes_and_spaces():
    comparisons, summary, _ = compare(
        [bill("HAWB-0001")], manifest([{"hawb_no": "HAWB 0001"}])
    )
    assert comparisons[0].manifest_entry_id is not None
    assert summary.consistent == 1


def test_weight_mismatch_marks_inconsistent():
    comparisons, summary, _ = compare(
        [bill()], manifest([{"hawb_no": "HAWB0001", "weight": "200.0"}])
    )
    comparison = comparisons[0]
    assert comparison.status == "inconsistent"
    weight = next(item for item in comparison.fields if item.key == "weight")
    assert weight.status == "mismatch"
    assert summary.inconsistent == 1


def test_weight_within_tolerance_is_match():
    comparisons, _, _ = compare(
        [bill()], manifest([{"hawb_no": "HAWB0001", "weight": "187.0"}])
    )
    weight = next(item for item in comparisons[0].fields if item.key == "weight")
    assert weight.status == "match"


def test_port_alias_and_flight_format_are_normalized():
    comparisons, _, _ = compare(
        [bill()],
        manifest(
            [
                {
                    "hawb_no": "HAWB0001",
                    "origin_port": "上海浦东国际机场",
                    "dest_port": "Los Angeles",
                    "flight_no": "CA 1234",
                    "flight_date": "20OCT2026",
                }
            ]
        ),
    )
    for key in ("origin_port", "dest_port", "flight_no", "flight_date"):
        field = next(item for item in comparisons[0].fields if item.key == key)
        assert field.status == "match", key
    assert comparisons[0].status == "consistent"


def test_punctuation_difference_in_name_is_match():
    comparisons, _, _ = compare(
        [bill()],
        manifest(
            [
                {
                    "hawb_no": "HAWB0001",
                    "shipper_name": "SHANGHAI EXAMPLE TRADING CO LTD",
                }
            ]
        ),
    )
    name = next(item for item in comparisons[0].fields if item.key == "shipper_name")
    assert name.status == "match"


def test_similar_address_is_flagged_but_not_inconsistent():
    comparisons, _, _ = compare(
        [bill()],
        manifest(
            [
                {
                    "hawb_no": "HAWB0001",
                    "consignee_address": "1000 EXAMPLE BOULEVARD, LOS ANGELES, CA 90001",
                }
            ]
        ),
    )
    address = next(item for item in comparisons[0].fields if item.key == "consignee_address")
    assert address.status in {"similar", "match"}


def test_missing_manifest_value_marks_incomplete():
    comparisons, summary, _ = compare(
        [bill()], manifest([{"hawb_no": "HAWB0001", "shipper_email": None}])
    )
    assert comparisons[0].status == "incomplete"
    assert summary.incomplete == 1


def test_bill_without_manifest_row():
    comparisons, summary, warnings = compare([bill()], manifest([{"hawb_no": "HAWB9999"}]))
    assert comparisons[0].manifest_entry_id is None
    assert comparisons[0].status == "incomplete"
    assert summary.house_only == 1
    assert summary.manifest_only == 1
    assert any("未找到" in warning for warning in warnings)


def test_missing_manifest_document():
    comparisons, summary, _ = compare([bill()], None)
    assert summary.total_manifest_entries == 0
    assert comparisons[0].manifest_entry_id is None
    assert summary.house_only == 1


def test_duplicate_hawb_warns():
    _, _, warnings = compare(
        [bill("HAWB0001"), bill("HAWB0001")],
        manifest([{"hawb_no": "HAWB0001"}]),
    )
    assert any("重复" in warning for warning in warnings)


def test_empty_volume_on_both_sides_is_not_mismatch():
    comparisons, _, _ = compare(
        [bill(volume=None)], manifest([{"hawb_no": "HAWB0001", "volume": None}])
    )
    volume = next(item for item in comparisons[0].fields if item.key == "volume")
    assert volume.status == "missing_both"
    assert comparisons[0].status == "consistent"
