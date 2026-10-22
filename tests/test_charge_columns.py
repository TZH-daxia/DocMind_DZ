"""计价列兜底：Rate/Charge、Total 位置对不上就置空，避免模型拿别处数字凑数。"""

from __future__ import annotations

from app.service.pipeline import _drop_unreliable_charge_fields


def test_drops_values_outside_cargo_row() -> None:
    """空栏时模型把 DIMS 里的 1、毛重列的 440.00 当成了单价/总价。"""

    fields = {
        "jfweight": "440.0",
        "rate": "1",
        "ratetotal": "440.00",
    }
    locations = {
        "jfweight": [{"page": 2, "bbox": [0.3209, 0.4923, 0.042, 0.0163]}],
        # DIMS 137x105x170 里的 "1"，在货物行下方
        "rate": [{"page": 2, "bbox": [0.6902, 0.5219, 0.0084, 0.0163]}],
        # 毛重列里的 "440.00"，在计费重量左侧
        "ratetotal": [{"page": 2, "bbox": [0.1097, 0.4923, 0.0514, 0.0163]}],
    }

    warnings = _drop_unreliable_charge_fields(fields, locations)

    assert fields["rate"] is None
    assert fields["ratetotal"] is None
    assert "rate" not in locations
    assert "ratetotal" not in locations
    assert len(warnings) == 2


def test_keeps_values_in_the_right_cells() -> None:
    """第 2 份分单本行确实印了 4.00 / 6484.00，不能被误清。"""

    fields = {
        "jfweight": "1621.0",
        "rate": "4.00",
        "ratetotal": "6484.00",
    }
    locations = {
        "jfweight": [{"page": 3, "bbox": [0.3192, 0.4931, 0.0514, 0.0163]}],
        "rate": [{"page": 3, "bbox": [0.4532, 0.4931, 0.0327, 0.0163]}],
        "ratetotal": [{"page": 3, "bbox": [0.5825, 0.4931, 0.0607, 0.0163]}],
    }

    assert _drop_unreliable_charge_fields(fields, locations) == []
    assert fields["rate"] == "4.00"
    assert fields["ratetotal"] == "6484.00"


def test_skips_when_anchor_missing() -> None:
    """没有计费重量的定位结果时不做判断，避免误删正确值。"""

    fields = {"rate": "4.00"}
    locations = {"rate": [{"page": 3, "bbox": [0.4532, 0.4931, 0.0327, 0.0163]}]}

    assert _drop_unreliable_charge_fields(fields, locations) == []
    assert fields["rate"] == "4.00"
