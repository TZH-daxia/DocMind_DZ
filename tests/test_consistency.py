"""分单 ↔ 舱单核对。"""

from app.schemas.documents import ManifestDocument, ManifestEntry, ManifestMaster
from app.schemas.fields import HOUSE_FIELD_KEYS, MANIFEST_FIELD_KEYS, empty_field_map
from app.service.consistency import compare, compare_totals


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
            "companytitle_fhr_hawb": "SHANGHAI EXAMPLE TRADING CO., LTD",
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


def bill(hawb: str = "HAWB0001", **overrides: str | None) -> dict:
    return {"bill_id": f"bill_{hawb}", "fields": house_fields(hawb, **overrides)}


def manifest(rows: list[dict]) -> ManifestDocument:
    entries = [
        ManifestEntry(
            entry_id=f"mft_{index}",
            line_no=index,
            fields=manifest_fields(row.pop("hawb_no"), **row),
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


def test_port_alias_is_normalized():
    comparisons, _, _ = compare(
        [bill()],
        manifest([{"hawb_no": "HAWB0001", "dest_port": "Los Angeles"}]),
    )
    field = next(item for item in comparisons[0].fields if item.key == "dest_port")
    assert field.status == "match"
    assert comparisons[0].status == "consistent"


def test_name_similarity_ignores_punctuation():
    from app.service.normalizers import similarity

    assert (
        similarity(
            "SHANGHAI EXAMPLE TRADING CO., LTD",
            "SHANGHAI EXAMPLE TRADING CO LTD",
        )
        >= 0.9
    )


def test_address_similarity_marks_similar():
    from app.service.normalizers import similarity_address

    assert (
        similarity_address(
            "1000 EXAMPLE BLVD, LOS ANGELES, CA 90001",
            "1000 EXAMPLE BOULEVARD, LOS ANGELES, CA 90001",
        )
        >= 0.92
    )


def test_missing_manifest_value_does_not_affect_status():
    comparisons, summary, _ = compare(
        [bill()], manifest([{"hawb_no": "HAWB0001", "weight": None}])
    )
    # 缺值不再判「信息不全」，只保留字段级 missing（前端在舱单标未校验）
    assert comparisons[0].status == "consistent"
    assert summary.incomplete == 0
    weight = next(item for item in comparisons[0].fields if item.key == "weight")
    assert weight.status == "missing_in_manifest"


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


def totals_manifest(**overrides: str | None) -> ManifestDocument:
    values = {
        "mawb_no": "999-12345678",
        "dest_port": "LAX",
        "pieces": "20",
        "weight": "300",
        "chargeable_weight": "300",
        "volume": "2.00",
    }
    values.update(overrides)
    return ManifestDocument(
        segment_id="seg_manifest",
        entries=[
            ManifestEntry(entry_id="mft_1", line_no=1, fields=manifest_fields("HAWB0001"))
        ],
        master=ManifestMaster(**values),
    )


def test_totals_checks_all_match():
    bills = [
        bill("HAWB0001", zdpiece="12", zdweight="186.5", zdvolume="1.20"),
        bill("HAWB0002", zdpiece="8", zdweight="113.5", zdvolume="0.80"),
    ]
    for item in bills:
        item["fields"]["jfweight"] = item["fields"]["zdweight"]
    checks = {
        check.key: check
        for check in compare_totals(
            bills,
            totals_manifest(
                pieces="20",
                weight="300",
                chargeable_weight="300",
                volume="2.0",
            ),
        )
    }
    assert {key: check.status for key, check in checks.items()} == {
        "dest_port": "match",
        "pieces": "match",
        "weight": "match",
        "chargeable_weight": "match",
        "volume": "match",
    }


def test_totals_checks_detect_mismatch():
    bills = [
        bill("HAWB0001", zdpiece="12", zdweight="186.5", zdvolume="1.20", mdg="LAX"),
        bill("HAWB0002", zdpiece="8", zdweight="113.5", zdvolume="0.80", mdg="DFW"),
    ]
    for item in bills:
        item["fields"]["jfweight"] = "100"
    checks = {
        check.key: check
        for check in compare_totals(
            bills,
            totals_manifest(
                pieces="21",
                weight="400",
                chargeable_weight="250",
                volume="2.0",
            ),
        )
    }
    assert checks["dest_port"].status == "mismatch"
    assert checks["pieces"].status == "mismatch"
    assert checks["weight"].status == "mismatch"
    # 分单计费重量合计 200 小于总单 250
    assert checks["chargeable_weight"].status == "mismatch"
    assert checks["volume"].status == "match"


def test_totals_chargeable_weight_allows_greater_total():
    bills = [bill("HAWB0001", zdpiece="12", zdweight="186.5", zdvolume="1.20")]
    bills[0]["fields"]["jfweight"] = "210"
    checks = {
        check.key: check
        for check in compare_totals(
            bills,
            totals_manifest(pieces="12", weight="186.5", chargeable_weight="200", volume="1.2"),
        )
    }
    assert checks["chargeable_weight"].status == "match"


def test_totals_checks_missing_when_no_master():
    bills = [bill("HAWB0001")]
    manifest = ManifestDocument(segment_id="seg_manifest")
    checks = compare_totals(bills, manifest)
    assert {check.status for check in checks} == {"missing"}


def test_duplicate_hawb_warns():
    _, _, warnings = compare(
        [bill("HAWB0001"), bill("HAWB0001")],
        manifest([{"hawb_no": "HAWB0001"}]),
    )
    assert any("重复" in warning for warning in warnings)


def test_empty_volume_on_both_sides_is_not_mismatch():
    comparisons, _, _ = compare(
        [bill(zdvolume=None)], manifest([{"hawb_no": "HAWB0001", "volume": None}])
    )
    volume = next(item for item in comparisons[0].fields if item.key == "volume")
    assert volume.status == "missing_both"
    assert comparisons[0].status == "consistent"
