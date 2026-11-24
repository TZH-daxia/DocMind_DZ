"""字段归一化。"""

import pytest

from app.service.normalizers import (
    normalize_date,
    normalize_email,
    normalize_flight_no,
    normalize_hawb,
    normalize_number,
    normalize_phone,
)


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 舱单里常见的 "H-"/"HAWB-" 前缀与连字符都要清掉
        ("H-1077968462", "1077968462"),
        ("h-1077968462", "1077968462"),
        ("H - 1077 968462", "1077968462"),
        ("HAWB-1077968462", "1077968462"),
        ("HAWB:1077968462", "1077968462"),
        ("H-HAWB-1077968462", "1077968462"),
        ("分单号：1077968462", "1077968462"),
        ("MAWB 416-52331591", "41652331591"),
        ("416-52331591", "41652331591"),
        # 标签粘着长号时同样剥掉（HAWB1077968462 → 1077968462）
        ("HAWB1077968462", "1077968462"),
        # 纯数字 / 带英文前缀的正常号段原样保留
        ("1077968462", "1077968462"),
        ("EDC2037060", "EDC2037060"),
        ("H1234567", "H1234567"),
        # 短串不剥：HAWB0001 本身可能就是号，剥了只剩 0001
        ("HAWB0001", "HAWB0001"),
        (" 1077968462 ", "1077968462"),
        # 只有前缀/分隔符：视为没有分单号
        ("H-", None),
        ("HAWB", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_hawb(raw, expected):
    assert normalize_hawb(raw) == expected
