"""主单号（mawb）归一：AWB 左上角那串的几种写法都要能还原。"""

from app.service.extraction import _normalize_house_bill_payload
from app.service.normalizers import normalize_mawb


def test_normalize_mawb_iata_forms() -> None:
    # 3 位前缀 + 机场三字码 + 8 位序号（连写 / 带空格 / 带连字符）
    assert normalize_mawb("416PVG52331591") == "416-52331591"
    assert normalize_mawb("369 TSN 10799445") == "369-10799445"
    assert normalize_mawb("416-52331591") == "416-52331591"
    assert normalize_mawb("416 5233 1591") == "416-52331591"


def test_normalize_mawb_ignores_house_bill_numbers() -> None:
    # 分单号形状不符合「3 位 + 8 位」，不能被误当成主单号
    assert normalize_mawb("EDC2037060") is None
    assert normalize_mawb("1077968462") is None
    assert normalize_mawb("SHA71763620") is None
    assert normalize_mawb(None) is None
    assert normalize_mawb("") is None


def test_mawb_normalized_from_model_value() -> None:
    payload = {
        "fields": {"hawb": "EDC2037060", "mawb": "416PVG52331591"},
        "confidence": 0.9,
    }
    result = _normalize_house_bill_payload(payload)
    assert result["fields"]["mawb"] == "416-52331591"


def test_mawb_backfilled_from_evidence_when_value_missing() -> None:
    # 线上真实情况：模型把值只写进 evidence 的 quote，字段本身给了 null
    payload = {
        "fields": {"hawb": "EDC2037060", "mawb": None},
        "evidence": {"mawb": {"quote": "416-52331591"}},
        "confidence": 0.95,
    }
    result = _normalize_house_bill_payload(payload)
    assert result["fields"]["mawb"] == "416-52331591"
