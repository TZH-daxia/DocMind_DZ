"""本地演示用的假模型（DOCMIND_DZ_MOCK_LLM=true）。

只覆盖本项目的三种调用：页面分类、分单抽取、舱单抽取。返回固定样例：
2 份分单 + 3 行舱单（其中 1 份分单重量不一致、1 行舱单只有分单没有分单页），
用于在没有 API Key 的情况下验证管线、核对逻辑与前端展示。
"""

from __future__ import annotations

import json
import re
import time

from app.config import Settings

_MOCK_HOUSE_BILLS = [
    {
        "hawb_no": "MOCK0001",
        "origin_port": "PVG",
        "dest_port": "LAX",
        "flight_no": "CA1234",
        "flight_date": "2026-10-20",
        "pieces": "12",
        "weight": "186.5",
        "volume": "1.20",
        "shipper": {
            "name": "上海示例贸易有限公司",
            "address": "上海市浦东新区示例路 100 号",
            "phone": "021-58880000",
            "email": "shipper@example.com",
        },
        "consignee": {
            "name": "EXAMPLE TRADING INC.",
            "address": "1000 Example Blvd, Los Angeles, CA 90001",
            "phone": "+1 213 555 0100",
            "email": "consignee@example.com",
        },
    },
    {
        "hawb_no": "MOCK0002",
        "origin_port": "PVG",
        "dest_port": "LAX",
        "flight_no": "CA1234",
        "flight_date": "2026-10-20",
        "pieces": "8",
        "weight": "95.0",
        "volume": "0.80",
        "shipper": {
            "name": "苏州示例电子有限公司",
            "address": "江苏省苏州市工业园区示例大道 8 号",
            "phone": "0512-66880000",
            "email": "sales@example-sz.com",
        },
        "consignee": {
            "name": "SAMPLE ELECTRONICS LLC",
            "address": "2000 Sample Street, Los Angeles, CA 90002",
            "phone": "+1 213 555 0199",
            "email": "import@example-la.com",
        },
    },
]

_MOCK_MANIFEST = [
    {
        **_MOCK_HOUSE_BILLS[0],
        "line_no": 1,
    },
    {
        **_MOCK_HOUSE_BILLS[1],
        "weight": "115.0",  # 与分单不一致，用于演示核对差异
        "line_no": 2,
    },
    {
        "hawb_no": "MOCK0003",
        "origin_port": "PVG",
        "dest_port": "ORD",
        "flight_no": "CA1235",
        "flight_date": "2026-10-21",
        "pieces": "5",
        "weight": "60.0",
        "volume": "0.45",
        "shipper": {
            "name": "宁波示例服饰有限公司",
            "address": "宁波市鄞州区示例路 9 号",
            "phone": "0574-88000000",
            "email": "nb@example.com",
        },
        "consignee": {
            "name": "CHICAGO SAMPLE CO.",
            "address": "3000 Sample Ave, Chicago, IL 60601",
            "phone": "+1 312 555 0123",
            "email": "chi@example.com",
        },
        "line_no": 3,
    },
]


class MockModelGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._house_index = 0

    async def complete(self, label: str, system_prompt: str, human_prompt: str) -> str:
        await _sleep()
        if "分类" in label or "分类" in system_prompt:
            return self._classify(human_prompt)
        if "舱单" in label:
            return json.dumps(
                [{"line_no": row.pop("line_no"), **row} for row in _clone(_MOCK_MANIFEST)],
                ensure_ascii=False,
            )
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
        image_paths: list[str],
    ) -> str:
        await _sleep()
        return "（MOCK 模式）本页为图片/扫描件，未执行真实 OCR 转写。"

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
    _ = time.time()


def _clone(rows: list[dict]) -> list[dict]:
    return json.loads(json.dumps(rows, ensure_ascii=False))
