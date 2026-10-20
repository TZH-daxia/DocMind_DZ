"""字段归一化。"""

import pytest

from app.service.normalizers import (
    normalize_date,
    normalize_email,
    normalize_flight_no,
    normalize_number,
    normalize_phone,
    normalize_port,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PVG", "PVG"),
        ("pvg", "PVG"),
        ("上海浦东国际机场", "PVG"),
        ("Shanghai Pudong (PVG)", "PVG"),
        ("Los Angeles", "LAX"),
        ("LAX ", "LAX"),
        ("SZX", "SZX"),
    ],
)
def test_normalize_port(raw, expected):
    assert normalize_port(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CA 1234", "CA1234"),
        ("ca-1234", "CA1234"),
        ("CA1234 ", "CA1234"),
        ("3U 8888", "3U8888"),
    ],
)
def test_normalize_flight_no(raw, expected):
    assert normalize_flight_no(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-20", "2026-10-20"),
        ("2026/10/20", "2026-10-20"),
        ("20OCT2026", "2026-10-20"),
        ("20 OCT 2026", "2026-10-20"),
        ("20/10/2026", "2026-10-20"),
        ("2026年10月20日", "2026-10-20"),
        ("20261020", "2026-10-20"),
    ],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_normalize_phone_strips_country_code():
    assert normalize_phone("+86 138-0000-0000") == "13800000000"
    assert normalize_phone("0086 21 5888 0000") == "2158880000"
    assert normalize_phone("(021) 5888-0000") == "02158880000"


def test_normalize_number_with_units_and_thousands():
    assert normalize_number("1,234.50 KG") == 1234.5
    assert normalize_number("0.80 CBM") == 0.8
    assert normalize_number("") is None
    assert normalize_number("N/M") is None


def test_normalize_email():
    assert normalize_email(" Shipper@Example.COM ") == "shipper@example.com"
