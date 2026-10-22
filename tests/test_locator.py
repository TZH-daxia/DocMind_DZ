"""字段定位回归：件数不能被地址列、条码行里的数字抢走。"""

from __future__ import annotations

from pathlib import Path

from app.collector.locator import locate_fields, locate_manifest_fields

PAGE_WIDTH = 842
PAGE_HEIGHT = 595


def _build_pdf(
    path: Path,
    lines: list[tuple[float, float, str]],
    size: tuple[float, float] = (PAGE_WIDTH, PAGE_HEIGHT),
) -> Path:
    """按 (x, y, text) 写文本层；写入顺序即 PDF 文本流顺序。"""

    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=size[0], height=size[1])
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=8, fontname="helv")
    document.save(path)
    document.close()
    return path


def _box_origin(
    box: list[float], size: tuple[float, float] = (PAGE_WIDTH, PAGE_HEIGHT)
) -> tuple[float, float]:
    return box[0] * size[0], box[1] * size[1]


def test_pieces_not_taken_from_postal_code(tmp_path: Path) -> None:
    """文本流里地址列排在前面，件数仍应定位到 Pkg 单元格。"""

    pdf = _build_pdf(
        tmp_path / "manifest.pdf",
        [
            (300, 200, "215300"),
            (45, 100, "HAWB: EDC2037060"),
            (45, 121, "Wgt / Vol / Pkg: 440.00 KG / 2.450 M3 / 1 CTN"),
        ],
    )
    fields = {
        "hawb_no": "EDC2037060",
        "pieces": "1",
        "weight": "440.00",
        "volume": "2.450",
    }

    locations = locate_manifest_fields(pdf, {1: 1}, fields, anchor="EDC2037060")

    _, y = _box_origin(locations["pieces"][0]["bbox"])
    assert 110 < y < 130, "件数应落在 Wgt / Vol / Pkg 行，而不是地址列的 215300"


def test_pieces_not_taken_from_consignee_ein(tmp_path: Path) -> None:
    """EIN 43-134239000 与锚点行只差 9pt，件数仍取整词命中的 4。"""

    pdf = _build_pdf(
        tmp_path / "manifest.pdf",
        [
            (45, 100, "HAWB: SHA71763620"),
            (300, 109, "EIN 43-134239000"),
            (45, 121, "Wgt / Vol / Pkg: 1621.00 KG / 4.920 M3 / 4 PLP"),
        ],
    )
    fields = {
        "hawb_no": "SHA71763620",
        "pieces": "4",
        "weight": "1621.00",
        "volume": "4.920",
    }

    locations = locate_manifest_fields(pdf, {1: 1}, fields, anchor="SHA71763620")

    _, y = _box_origin(locations["pieces"][0]["bbox"])
    assert 110 < y < 130, "件数应落在 Pkg 单元格，而不是收货人 EIN 里的 4"


def test_dest_port_falls_back_to_substring(tmp_path: Path) -> None:
    """目的港印成 USDFW 时没有整词命中，应退回子串匹配而不是不定位。"""

    pdf = _build_pdf(
        tmp_path / "manifest.pdf",
        [
            (45, 100, "HAWB: EDC2037060"),
            (45, 121, "USDFW"),
        ],
    )
    fields = {"hawb_no": "EDC2037060", "dest_port": "DFW"}

    locations = locate_manifest_fields(pdf, {1: 1}, fields, anchor="EDC2037060")

    assert locations.get("dest_port"), "USDFW 中的 DFW 应能被正确定位"


def test_flight_date_covers_day_split_from_flight_no(tmp_path: Path) -> None:
    """日期和航班号连排（N8652/30 + SEP/26）时，高亮要连「日」一起框住。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (134, 340, "N8652/30"),
            (205, 340, "SEP/26"),
        ],
        size=size,
    )

    locations = locate_fields(pdf, {1: 1}, {"flight_date": "2026-09-30"})

    box = locations["flight_date"][0]["bbox"]
    import pymupdf

    with pymupdf.open(pdf) as document:
        page = document.load_page(0)
        clip = pymupdf.Rect(
            box[0] * size[0],
            box[1] * size[1],
            (box[0] + box[2]) * size[0] + 4,
            (box[1] + box[3]) * size[1],
        )
        highlighted = page.get_text("text", clip=clip)

    assert "30" in highlighted, "日没有被框进高亮"
    assert "SEP" in highlighted


def test_house_bill_pieces_anchored_by_label(tmp_path: Path) -> None:
    """分单 Pieces 要用 No. Of Pieces 标签锚定，不能被条码行的 CN1 抢走。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (150, 10, "CN1HBLPVGDFW"),  # 顶部条码/标题行，文本流最前
            (500, 440, "DIMS 137x105x170 CM x 1"),  # 品名栏里的另一个 1
            (27, 397, "No."),
            (37, 397, "Of"),
            (27, 403, "Pieces"),
            (29, 409, "RCP"),
            (22, 421, "1"),  # RCP 格子的真实件数
        ],
        size=size,
    )

    locations = locate_fields(pdf, {1: 1}, {"pieces": "1"})

    x, y = _box_origin(locations["pieces"][0]["bbox"], size)
    assert x < 60 and 405 < y < 430, "件数应落在 RCP 格子，而不是页首条码行"


def test_companycode_uses_exact_word_and_party_block(tmp_path: Path) -> None:
    """国家二字码只认整词，且要落在各自的收发货人块里，不能被条码里的 CN 抢走。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 60, "CN1HBLPVGDFW"),  # 页首条码，含 CN 但不是独立词
            (40, 300, "SHIPPER'S NAME AND ADDRESS"),
            (40, 330, "KUNSHAN 215314 CN"),
            (40, 500, "CONSIGNEE'S NAME AND ADDRESS"),
            (40, 530, "BROWNSVILLE TX 78521 US"),
        ],
        size=size,
    )

    locations = locate_fields(
        pdf,
        {1: 1},
        {"companycode_fhr_hawb": "CN", "companycode_shr_hawb": "US"},
    )

    _, shipper_y = _box_origin(locations["companycode_fhr_hawb"][0]["bbox"], size)
    _, consignee_y = _box_origin(locations["companycode_shr_hawb"][0]["bbox"], size)
    assert 320 < shipper_y < 345, "发货人国家代码应落在地址下方同一行"
    assert 520 < consignee_y < 545, "收货人国家代码应落在自己的地址块里"
