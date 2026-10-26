"""模型输出适配：同义键名归一与舱单数组兼容。"""

import json

from app.schemas.documents import PageRecord
from app.schemas.fields import HOUSE_FIELD_KEYS
from app.service.extraction import (
    _normalize_house_bill_payload,
    canonical_payload,
    extract_manifest,
)


def test_alias_keys_are_normalized():
    payload = {
        "hawb": "SHA1234567",
        "origin": "PVG",
        "destination": "LAX",
        "flight": "CA 1234",
        "etd": "20OCT2026",
        "pcs": "12",
        "gross_weight": "186.5 KG",
        "cbm": "1.20",
        "shipper": "SHANGHAI EXAMPLE CO",
        "consignee": {
            "company": "EXAMPLE TRADING INC.",
            "addr": "1000 EXAMPLE BLVD, LOS ANGELES",
            "tel": "+1 213 555 0100",
            "mail": "CONSIGNEE@EXAMPLE.COM",
        },
    }
    result = _normalize_house_bill_payload(payload)
    fields = result["fields"]

    assert fields["hawb"] == "SHA1234567"
    assert fields["sfg"] == "PVG"
    assert fields["mdg"] == "LAX"
    assert fields["hbh"] == "CA 1234"
    assert fields["hbrq"] == "20OCT2026"
    assert fields["companytitle_fhr_hawb"] == "SHANGHAI EXAMPLE CO"
    assert fields["companytitle_shr_hawb"] == "EXAMPLE TRADING INC."
    assert fields["phone_shr_hawb"] == "12135550100"
    assert fields["email_shr_hawb"] == "CONSIGNEE@EXAMPLE.COM"


def test_phone_drops_plus_and_china_prefix():
    """手机号只留数字：+86 开头去掉 86，其余只去掉 +。"""

    fields = _normalize_house_bill_payload(
        {
            "fields": {
                "hawb": "HAWB0001",
                "phone_fhr_hawb": "+8651257382098",
                "phone_shr_hawb": "+18649636328",
            }
        }
    )["fields"]

    assert fields["phone_fhr_hawb"] == "51257382098"
    assert fields["phone_shr_hawb"] == "18649636328"


def test_canonical_payload_keeps_standard_structure():
    canonical = canonical_payload(
        {
            "hawb_no": "HAWB0001",
            "shipper": {"name": "A", "address": "B", "phone": "C", "email": "D"},
            "consignee": {"name": "E"},
        }
    )
    assert canonical["hawb_no"] == "HAWB0001"
    assert canonical["shipper"] == {"name": "A", "address": "B", "phone": "C", "email": "D"}
    assert canonical["consignee"] == {"name": "E"}


class _FakeArrayGateway:
    async def complete(self, label, system_prompt, human_prompt):
        return json.dumps(
            [
                {
                    "line_no": 1,
                    "fields": {"hawb": "HAWB0001", "origin": "PVG", "pcs": "12"},
                }
            ],
            ensure_ascii=False,
        )


async def test_manifest_accepts_raw_array_response():
    pages = [PageRecord(index=1, text="CARGO MANIFEST\nHAWB0001 | PVG")]
    result = await extract_manifest(_FakeArrayGateway(), pages, [1])
    assert len(result["entries"]) == 1
    assert result["entries"][0]["fields"]["hawb_no"] == "HAWB0001"
    assert result["entries"][0]["fields"]["pieces"] == "12"
    # 舱单只保留核对字段，始发港等不再输出
    assert "origin_port" not in result["entries"][0]["fields"]


class _FakeManifestDestinationGateway:
    async def complete(self, label, system_prompt, human_prompt):
        return json.dumps(
            {
                "entries": [
                    {
                        "line_no": 1,
                        "fields": {
                            "hawb_no": "HAWB0001",
                            "dest_port": "DFW",
                            "chargeable_weight": "2061.00",
                            "pieces": "5",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        )


async def test_manifest_keeps_destination_and_chargeable_weight():
    """舱单口径的 dest_port/chargeable_weight 不能被分单接口名映射吃掉。"""

    pages = [PageRecord(index=1, text="CARGO MANIFEST")]
    result = await extract_manifest(_FakeManifestDestinationGateway(), pages, [1])
    fields = result["entries"][0]["fields"]

    assert fields["dest_port"] == "DFW"
    assert fields["chargeable_weight"] == "2061.00"
    assert fields["pieces"] == "5"


class _FakeMasterRowGateway:
    async def complete(self, label, system_prompt, human_prompt):
        return json.dumps(
            {
                "entries": [
                    {
                        "line_no": 1,
                        "row_type": "M",
                        "fields": {"hawb": "272-76217971", "pieces": "45"},
                    },
                    {
                        "line_no": 2,
                        "row_type": "H",
                        "fields": {"hawb": "WPL26100101", "pieces": "45", "cbm": "11.007"},
                    },
                    {
                        # 没有 row_type，但填的是 11 位主单号
                        "line_no": 3,
                        "fields": {"hawb": "416-52342905", "pieces": "42"},
                    },
                ]
            },
            ensure_ascii=False,
        )


async def test_manifest_skips_master_rows():
    pages = [PageRecord(index=1, text="CARGO MANIFEST")]
    result = await extract_manifest(_FakeMasterRowGateway(), pages, [1])
    assert [entry["fields"]["hawb_no"] for entry in result["entries"]] == ["WPL26100101"]


def test_house_candidates_keep_evidence_and_missing():
    """候选层：有值字段保留 quote/status/confidence，缺失字段补 missing。"""

    payload = {
        "fields": {"hawb": "HAWB0001", "sfg": "PVG"},
        "evidence": {
            "hawb": {
                "quote": "HAWB: HAWB0001",
                "status": "confirmed",
                "confidence": 0.95,
            },
            "sfg": {
                "quote": "Airport of Departure PVG",
                "status": "confirmed",
                "confidence": 0.9,
            },
        },
        "confidence": 0.8,
    }

    result = _normalize_house_bill_payload(payload)
    candidates = {item["field_key"]: item for item in result["candidates"]}

    assert candidates["hawb"]["value"] == "HAWB0001"
    assert candidates["hawb"]["evidence"][0]["quote"] == "HAWB: HAWB0001"
    assert candidates["hawb"]["confidence"] == 0.95
    assert candidates["mdg"]["status"] == "missing"
    assert candidates["mdg"]["value"] is None
    # 全部字段都应有候选，缺值字段也不能少
    assert len(result["candidates"]) == len(HOUSE_FIELD_KEYS)
