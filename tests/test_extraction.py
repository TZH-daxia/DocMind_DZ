"""模型输出适配：同义键名归一与舱单数组兼容。"""

import json

from app.schemas.documents import PageRecord
from app.schemas.fields import HAWB_UNRECOGNIZED, HOUSE_FIELD_KEYS
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


def test_house_bill_hawb_strips_prefix_and_dash():
    """分单号里的「H-」「HAWB-」前缀与连字符都要清掉。"""

    fields = _normalize_house_bill_payload(
        {"fields": {"hawb": "HAWB-1077968462"}}
    )["fields"]

    assert fields["hawb"] == "1077968462"


def test_house_bill_without_hawb_fills_placeholder():
    """有主单号、但分单号读不出来：仍产出单据，分单号填「识别失败」。"""

    result = _normalize_house_bill_payload(
        {"fields": {"mawb": "999-12345678", "sfg": "PVG"}}
    )

    assert result["fields"]["hawb"] == HAWB_UNRECOGNIZED
    assert any("识别失败" in warning for warning in result["warnings"])


class _FakeHawbNoiseGateway:
    """舱单里分单号写成 H-xxxx，汇总行写成 MAWB 416-xxxx。"""

    async def complete(self, label, system_prompt, human_prompt):
        return json.dumps(
            {
                "entries": [
                    {"line_no": 1, "fields": {"hawb": "H-1077968462", "pieces": "10"}},
                    {"line_no": 2, "fields": {"hawb": "HAWB-1077 968462", "pieces": "2"}},
                    {"line_no": 3, "fields": {"hawb": "MAWB 416-52342905", "pieces": "12"}},
                ]
            },
            ensure_ascii=False,
        )


async def test_manifest_hawb_strips_prefix_and_dash():
    pages = [PageRecord(index=1, text="CARGO MANIFEST")]
    result = await extract_manifest(_FakeHawbNoiseGateway(), pages, [1])

    hawb_nos = [entry["fields"]["hawb_no"] for entry in result["entries"]]
    # 前两行清洗成同一号码；第三行是主单号，仍按总运单行跳过
    assert hawb_nos == ["1077968462", "1077968462"]
    assert all("-" not in value for value in hawb_nos)


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
                            "mawb": "999-12345678",
                            "hawb_no": "HAWB0001",
                            "sfg": "PVG",
                            "dest_port": "DFW",
                            "hbh": "K4917",
                            "hbrq": "2026-10-05",
                            "chargeable_weight": "2061.00",
                            "pieces": "5",
                            "shipper": {
                                "name": "SHANGHAI EXAMPLE",
                                "address": "NO.572 QINGYANG ROAD | KUNSHAN CITY",
                                "phone": "+86 21 5888 0000",
                            },
                        },
                    }
                ]
            },
            ensure_ascii=False,
        )


async def test_manifest_keeps_new_fields_and_drops_removed_ones():
    """舱单字段按新口径保留：单号/港口/收发货人；去掉的计费重量不再输出。"""

    pages = [PageRecord(index=1, text="CARGO MANIFEST")]
    result = await extract_manifest(_FakeManifestDestinationGateway(), pages, [1])
    fields = result["entries"][0]["fields"]

    assert fields["mawb"] == "999-12345678"
    assert fields["sfg"] == "PVG"
    assert fields["dest_port"] == "DFW"
    assert fields["hbh"] == "K4917"
    assert fields["hbrq"] == "2026-10-05"
    assert fields["pieces"] == "5"
    # 收发货人嵌套对象要展开成接口字段名（舱单保留原文，不做手机号归一）
    assert fields["companytitle_fhr_hawb"] == "SHANGHAI EXAMPLE"
    assert fields["phone_fhr_hawb"] == "+86 21 5888 0000"
    # 单元格里的 `|` 换行要换成空格，与分单口径一致
    assert fields["address_fhr_hawb"] == "NO.572 QINGYANG ROAD KUNSHAN CITY"
    assert not any("|" in (value or "") for value in fields.values())
    # 计费重量已不再抽取（后端不提供，前端人工填）
    assert "chargeable_weight" not in fields


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


class _FakeManifestMasterRowGateway:
    """模型把总运单汇总行也当成数据行输出（AWB 号列写 MAWB，没有分单号）。"""

    async def complete(self, label, system_prompt, human_prompt):
        return json.dumps(
            {
                "entries": [
                    {
                        "line_no": 1,
                        "fields": {
                            "mawb": "416-52342905",
                            "pieces": "42",
                            "weight": "2776",
                            "pm": "CONSOLIDATION SHIPMENT DETAILS AS PER ATTACHED MANIFEST",
                        },
                    },
                    {
                        "line_no": 2,
                        "fields": {
                            "mawb": "416-52342905",
                            "hawb_no": "SZA26100005",
                            "pieces": "42",
                            "weight": "2776",
                            "pm": "FIBER OPTIC PATCHCORD",
                        },
                    },
                ]
            },
            ensure_ascii=False,
        )


async def test_manifest_master_row_without_hawb_is_skipped():
    """没有分单号的行是总运单汇总行：直接跳过，不再产出 master 汇总块。"""

    pages = [PageRecord(index=1, text="AIR CARGO MANIFEST")]
    result = await extract_manifest(_FakeManifestMasterRowGateway(), pages, [1])

    assert len(result["entries"]) == 1
    assert result["entries"][0]["fields"]["hawb_no"] == "SZA26100005"
    assert "master" not in result
    assert any("没有分单号" in warning for warning in result["warnings"])

class _FakeManifestMissingHawbGateway:
    """舱单里有数据行，但某几行的分单号读不出来（主单号是有的）。"""

    async def complete(self, label, system_prompt, human_prompt):
        return json.dumps(
            {
                "entries": [
                    {
                        "line_no": 1,
                        "fields": {
                            "mawb": "999-12345678",
                            "pieces": "12",
                            "weight": "186.5",
                        },
                    },
                    {
                        "line_no": 2,
                        "fields": {
                            "mawb": "999-12345678",
                            "hawb_no": "1077968462",
                            "pieces": "8",
                        },
                    },
                ]
            },
            ensure_ascii=False,
        )


async def test_manifest_row_without_hawb_is_kept_with_placeholder():
    """没有分单号的数据行不再被丢弃：值填「识别失败」，保证整票数据不丢。"""

    pages = [PageRecord(index=1, text="CARGO MANIFEST")]
    result = await extract_manifest(_FakeManifestMissingHawbGateway(), pages, [1])

    assert [entry["fields"]["hawb_no"] for entry in result["entries"]] == [
        HAWB_UNRECOGNIZED,
        "1077968462",
    ]
    assert any("识别失败" in warning for warning in result["warnings"])


def test_manual_only_fields_are_not_extracted_at_all():
    """计费重量与申报价值不再抽取：这两个 key 不进入字段表，前端仍可人工填。"""

    from app.service.extraction import _normalize_house_bill_payload

    payload = {
        "fields": {
            "hawb": "HAWB0001",
            "jfweight": "440.0",
            "declarecustom": "6790.00 USD",
            "zdpiece": "1",
        }
    }

    result = _normalize_house_bill_payload(payload)

    assert "jfweight" not in result["fields"]
    assert "declarecustom" not in result["fields"]
    assert result["fields"]["zdpiece"] == "1"

    candidate_keys = {item["field_key"] for item in result["candidates"]}
    assert "jfweight" not in candidate_keys
    assert "declarecustom" not in candidate_keys
