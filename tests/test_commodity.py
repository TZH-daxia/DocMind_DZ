"""品名栏清洗：剔除 HS CODE、发票号、P.O. 等附加行。"""

from app.service.pipeline import _align_candidates, clean_commodity_description


def test_removes_hs_code_and_invoice_ref():
    value = "1262803_REV1P9_ZF_RK_F_BRK _ASM_ IN.4100159617 HS CODE: 847330"

    cleaned, changed = clean_commodity_description(value)

    assert cleaned == "1262803_REV1P9_ZF_RK_F_BRK _ASM_"
    assert changed is True


def test_removes_po_ref_and_multiline_extras():
    value = "REFRIGERANT PUMP\nP.O. NO 12345\nHS CODE: 8415"

    cleaned, changed = clean_commodity_description(value)

    assert cleaned == "REFRIGERANT PUMP"
    assert changed is True


def test_keeps_normal_description():
    value = "TEMPERATURE&HUMIDITY CHAMBER 1262803"

    assert clean_commodity_description(value) == (value, False)


def test_empty_value_untouched():
    assert clean_commodity_description(None) == (None, False)
    assert clean_commodity_description("") == ("", False)


def test_pm_candidate_stays_confirmed_after_cleaning():
    """品名清洗是规则动作，不应把候选降级成待审核。"""

    candidates = [
        {
            "field_key": "pm",
            "value": "GOODS HS CODE: 123",
            "raw_value": "GOODS HS CODE: 123",
            "status": "confirmed",
            "confidence": 0.9,
            "evidence": [{"quote": "GOODS HS CODE: 123"}],
            "warnings": [],
        }
    ]

    updated = _align_candidates(candidates, {"pm": "GOODS"})

    assert updated[0]["value"] == "GOODS"
    assert updated[0]["status"] == "confirmed"
    assert any("品名" in item for item in updated[0]["warnings"])
