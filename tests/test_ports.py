"""港口字段口径：只保留三字码，确定不了则留空并带回原文。"""

from app.service.ports import normalize_port_fields, resolve_port


def test_three_letter_code_is_kept():
    assert resolve_port("PVG") == ("PVG", "")
    assert resolve_port("pvg") == ("PVG", "")


def test_airport_alias_is_normalized():
    assert resolve_port("LOS ANGELES") == ("LAX", "")
    assert resolve_port("SHANGHAI PUDONG") == ("PVG", "")


def test_airport_full_name_is_resolved():
    assert resolve_port("SHANGHAI PUDONG INTERNATIONAL APT") == ("PVG", "")
    assert resolve_port("DALLAS-FORT WORTH INT") == ("DFW", "")


def test_ambiguous_city_keeps_raw_text():
    code, raw = resolve_port("SHANGHAI")
    assert code is None
    assert raw == "SHANGHAI"


def test_empty_value_returns_none():
    assert resolve_port(None) == (None, "")
    assert resolve_port("   ") == (None, "")


def test_normalize_port_fields_collects_evidence():
    fields = {"sfg": "PVG", "mdg": "SHANGHAI", "pieces": "1"}
    evidence = normalize_port_fields(fields)

    assert fields["sfg"] == "PVG"
    assert fields["mdg"] is None
    assert evidence == {"mdg": "SHANGHAI"}
    assert fields["pieces"] == "1"
