"""测试公共夹具：临时数据目录、mock 模型、样例 PDF 生成。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _clean_root_logger():
    import logging

    for handler in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(handler)
    yield


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        log_dir=tmp_path / "logs",
        mock_llm=True,
        house_bill_concurrency=1,
        text_layer_min_chars=20,
        model_api_key="test-key",
    )


MASTER_PAGE = """MASTER AIR WAYBILL
MAWB NO.: 999-12345678
Issued by: EXAMPLE AIR CARGO
Shipper: AAA TRADING CO., LTD
Consignee: BBB LOGISTICS INC.
Origin: PVG
Destination: LAX
Flight: CA1234
Pieces: 20   Gross Weight: 281.5 KG
"""

HOUSE_PAGE_TEMPLATE = """HOUSE AIR WAYBILL
HAWB NO.: {hawb}
MAWB NO.: 999-12345678
Shipper: {shipper}
Address: {shipper_address}
Tel: {shipper_phone}
Email: {shipper_email}
Consignee: {consignee}
Address: {consignee_address}
Tel: {consignee_phone}
Email: {consignee_email}
Origin: PVG
Destination: LAX
Flight: CA1234
Flight Date: 2026-10-20
Pieces: {pieces}   Gross Weight: {weight} KG
Volume: {volume} CBM
"""

MANIFEST_PAGE = """CARGO MANIFEST
HAWB NO. | ORIGIN | DEST | FLIGHT | FLIGHT DATE | PCS | G.W. | CBM
HAWB0001 | PVG | LAX | CA1234 | 2026-10-20 | 12 | 186.5 | 1.20
HAWB0002 | PVG | LAX | CA1234 | 2026-10-20 | 8 | 95.0 | 0.80
"""


def house_page(hawb: str, **overrides: str) -> str:
    payload = {
        "hawb": hawb,
        "shipper": "SHANGHAI EXAMPLE TRADING CO., LTD",
        "shipper_address": "100 EXAMPLE ROAD, SHANGHAI, CHINA",
        "shipper_phone": "+86 21 5888 0000",
        "shipper_email": "SHIPPER@EXAMPLE.COM",
        "consignee": "EXAMPLE TRADING INC.",
        "consignee_address": "1000 EXAMPLE BLVD, LOS ANGELES, CA 90001",
        "consignee_phone": "+1 213 555 0100",
        "consignee_email": "CONSIGNEE@EXAMPLE.COM",
        "pieces": "12",
        "weight": "186.5",
        "volume": "1.20",
    }
    payload.update(overrides)
    return HOUSE_PAGE_TEMPLATE.format(**payload)


def write_pdf(path: Path, page_texts: list[str]) -> Path:
    """用 PyMuPDF 生成带文本层的 PDF（无需外部字体）。"""

    import pymupdf

    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page(width=595, height=842)
        y = 60
        for line in text.splitlines():
            page.insert_text((45, y), line, fontsize=8, fontname="helv")
            y += 14
    document.save(path)
    document.close()
    return path
