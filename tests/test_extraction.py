"""模型输出适配：同义键名归一与舱单数组兼容。"""

import json

from app.schemas.documents import PageRecord
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

    assert fields["hawb_no"] == "SHA1234567"
    assert fields["origin_port"] == "PVG"
    assert fields["dest_port"] == "LAX"
    assert fields["flight_no"] == "CA 1234"
    assert fields["flight_date"] == "20OCT2026"
    assert fields["pieces"] == "12"
    assert fields["weight"] == "186.5 KG"
    assert fields["volume"] == "1.20"
    assert fields["shipper_name"] == "SHANGHAI EXAMPLE CO"
    assert fields["consignee_name"] == "EXAMPLE TRADING INC."
    assert fields["consignee_phone"] == "+1 213 555 0100"
    assert fields["consignee_email"] == "CONSIGNEE@EXAMPLE.COM"


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
    assert result["entries"][0]["fields"]["origin_port"] == "PVG"
    assert result["entries"][0]["fields"]["pieces"] == "12"


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
