"""测试替身：固定样例的假模型（不调用真实模型）。

覆盖本项目的三种调用：页面分类、分单抽取、舱单抽取。返回固定样例：
2 份分单 + 3 行舱单（其中 1 份分单重量不一致、1 行舱单只有分单没有分单页），
用于离线验证管线、核对逻辑与前端展示。
"""

from __future__ import annotations

import json
import re

from app.config import Settings

# 分单按单证接口字段名输出（件数/重量/体积只有制单口径 zd*）
_MOCK_HOUSE_BILLS = [
    {
        "hawb": "MOCK0001",
        "sfg": "PVG",
        "mdg": "LAX",
        "hbh": "CA1234",
        "hbrq": "2026-10-20",
        "currency": "CNY",
        "companytitle_fhr_hawb": "上海示例贸易有限公司",
        "address_fhr_hawb": "上海市浦东新区示例路 100 号",
        "city_fhr_hawb": "SHANGHAI",
        "companycode_fhr_hawb": "CN",
        "phone_fhr_hawb": "021-58880000",
        "email_fhr_hawb": "shipper@example.com",
        "companytitle_shr_hawb": "EXAMPLE TRADING INC.",
        "address_shr_hawb": "1000 Example Blvd, Los Angeles, CA 90001",
        "city_shr_hawb": "LOS ANGELES",
        "companycode_shr_hawb": "US",
        "state_shr_hawb": "CA",
        "phone_shr_hawb": "+1 213 555 0100",
        "email_shr_hawb": "consignee@example.com",
        "zdpiece": "12",
        "zdweight": "186.5",
        "ratelevel": "Q",
        "jfweight": "186.5",
        "zdvolume": "1.20",
        "pm": "MOCK CARGO",
    },
    {
        "hawb": "MOCK0002",
        "sfg": "PVG",
        "mdg": "LAX",
        "hbh": "CA1234",
        "hbrq": "2026-10-20",
        "currency": "CNY",
        "companytitle_fhr_hawb": "苏州示例电子有限公司",
        "address_fhr_hawb": "江苏省苏州市工业园区示例大道 8 号",
        "city_fhr_hawb": "SUZHOU",
        "companycode_fhr_hawb": "CN",
        "phone_fhr_hawb": "0512-66880000",
        "email_fhr_hawb": "sales@example-sz.com",
        "companytitle_shr_hawb": "SAMPLE ELECTRONICS LLC",
        "address_shr_hawb": "2000 Sample Street, Los Angeles, CA 90002",
        "city_shr_hawb": "LOS ANGELES",
        "companycode_shr_hawb": "US",
        "state_shr_hawb": "CA",
        "phone_shr_hawb": "+1 213 555 0199",
        "email_shr_hawb": "import@example-la.com",
        "zdpiece": "8",
        "zdweight": "95.0",
        "ratelevel": "Q",
        "jfweight": "95.0",
        "zdvolume": "0.80",
        "pm": "MOCK CARGO",
    },
]

# 舱单按舱单口径（match 键）输出
_MOCK_MANIFEST = [
    {
        "line_no": 1,
        "hawb_no": "MOCK0001",
        "dest_port": "LAX",
        "pieces": "12",
        "weight": "186.5",
        "volume": "1.20",
    },
    {
        "line_no": 2,
        "hawb_no": "MOCK0002",
        "dest_port": "LAX",
        "pieces": "8",
        "weight": "115.0",  # 与分单不一致，用于演示核对差异
        "volume": "0.80",
    },
    {
        "line_no": 3,
        "hawb_no": "MOCK0003",
        "dest_port": "ORD",
        "pieces": "5",
        "weight": "60.0",
        "volume": "0.45",
    },
]


class FakeModelGateway:
    """接口与 ModelGateway 一致的假网关，供测试离线使用。"""
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._house_index = 0

    async def complete(self, label: str, system_prompt: str, human_prompt: str) -> str:
        await _sleep()
        if "分类" in label or "分类" in system_prompt:
            return self._classify(human_prompt)
        if "舱单" in label:
            return json.dumps(_clone(_MOCK_MANIFEST), ensure_ascii=False)
        if "分单" in label:
            index = self._house_index % len(_MOCK_HOUSE_BILLS)
            self._house_index += 1
            bill = _clone(_MOCK_HOUSE_BILLS)[index]
            return json.dumps(bill, ensure_ascii=False)
        return "{}"

    async def complete_vision(
        self,
        label: str,
        system_prompt: str,
        human_prompt: str,
        image_paths: list[str] | None = None,
        *,
        image_bytes: list[bytes] | None = None,
    ) -> str:
        # 测试桩：返回空串表示"不覆盖文本层"，避免假 OCR 文本污染分类/抽取
        await _sleep()
        return ""

    def _classify(self, human_prompt: str) -> str:
        page_count = max(1, len(re.findall(r"^PAGE\s+\d+", human_prompt, flags=re.MULTILINE)))
        result = []
        for index in range(1, page_count + 1):
            if index == 1:
                kind, doc_no = "master_bill", "999-12345678"
            elif index == page_count:
                kind, doc_no = "manifest", None
            else:
                kind, doc_no = "house_bill", f"MOCK{self._house_index + index - 1:04d}"
            result.append(
                {
                    "page": index,
                    "kind": kind,
                    "doc_no": doc_no,
                    "continuation": False,
                    "confidence": 0.9,
                    "reason": "MOCK 分类",
                }
            )
        return json.dumps(result, ensure_ascii=False)


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.05)


def _clone(rows: list[dict]) -> list[dict]:
    return json.loads(json.dumps(rows, ensure_ascii=False))
