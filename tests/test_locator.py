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


def test_overlapping_label_does_not_block_value(tmp_path: Path) -> None:
    """标签与值框重叠（紧凑版式）时仍要定位，不能直接判死。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 100, "SHIPPER'S NAME"),
            (40, 104, "ACME TRADING CO"),
        ],
        size=size,
    )

    locations = locate_fields(pdf, {1: 1}, {"companytitle_fhr_hawb": "ACME TRADING CO"})

    x, y = _box_origin(locations["companytitle_fhr_hawb"][0]["bbox"], size)
    assert x < 120 and 95 < y < 115


def test_country_code_falls_back_to_full_name(tmp_path: Path) -> None:
    """单证印国家全称、抽取结果是二字码时，应定位到全称而不是页面别处的同形词。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 100, "SHIPPER'S NAME AND ADDRESS"),
            (40, 130, "NO.36 NORTH STREET"),
            (40, 140, "300301 TIANJIN"),
            (40, 150, "CHINA"),
            (534, 300, "CN"),
        ],
        size=size,
    )

    locations = locate_fields(pdf, {1: 1}, {"companycode_fhr_hawb": "CN"})

    _, y = _box_origin(locations["companycode_fhr_hawb"][0]["bbox"], size)
    assert 140 < y < 155


def test_city_stays_in_own_party_block(tmp_path: Path) -> None:
    """同一个城市名在代理行也出现时，城市要落在自己的地址块里。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 60, "KUEHNE + NAGEL LTD. TIANJIN BRANCH"),
            (40, 100, "SHIPPER'S NAME"),
            (40, 104, "ACME TRADING CO"),
            (40, 130, "NO.36 NORTH STREET"),
            (40, 140, "300301 TIANJIN"),
        ],
        size=size,
    )

    locations = locate_fields(
        pdf,
        {1: 1},
        {"companytitle_fhr_hawb": "ACME TRADING CO", "city_fhr_hawb": "TIANJIN"},
    )

    _, y = _box_origin(locations["city_fhr_hawb"][0]["bbox"], size)
    assert 128 < y < 145


def test_long_value_split_across_lines_uses_prefix(tmp_path: Path) -> None:
    """品名被页面换行拆开（模型拼成下划线连写）时，用最长可命中前缀兜底。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (430, 370, "NATURE AND QUANTITY OF GOODS"),
            (400, 390, "1262803_REV1P9_ZF_RK_F_BRK"),
            (400, 400, "_ASM_"),
            (400, 420, "HS CODE: 847330"),
        ],
        size=size,
    )

    locations = locate_fields(
        pdf,
        {1: 1},
        {"pm": "1262803_REV1P9_ZF_RK_F_BRK_ASM_ HS CODE: 847330"},
    )

    _, y = _box_origin(locations["pm"][0]["bbox"], size)
    assert 380 < y < 395


def test_flight_date_day_only_value(tmp_path: Path) -> None:
    """航班日期只印“日”（5Y518/22）时，也要定位到航班号的日期段。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 88, "22"),
            (40, 300, "REQUESTED FLIGHT/DATE"),
            (40, 320, "5Y518/22"),
        ],
        size=size,
    )

    locations = locate_fields(pdf, {1: 1}, {"hbrq": "2026-10-22"})

    _, y = _box_origin(locations["hbrq"][0]["bbox"], size)
    assert 306 < y < 320


def test_quote_anchor_chooses_nearby_value(tmp_path: Path) -> None:
    """同一个值出现在页面两处时，用原文引用锚点选正确那一处。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 300, "Airport of Departure LAX"),
            (420, 320, "LAX"),
        ],
        size=size,
    )

    locations = locate_fields(
        pdf,
        {1: 1},
        {"sfg": "LAX"},
        quotes={"sfg": "Airport of Departure LAX"},
    )

    x, _ = _box_origin(locations["sfg"][0]["bbox"], size)
    assert x < 150


def test_quote_fallback_when_value_not_found(tmp_path: Path) -> None:
    """值在页面上找不到时，退化为唯一命中的引用位置。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [(40, 300, "Airport of Departure SHANGHAI")],
        size=size,
    )

    locations = locate_fields(
        pdf,
        {1: 1},
        {"sfg": "PVG"},
        quotes={"sfg": "Airport of Departure SHANGHAI"},
    )

    assert "sfg" in locations


def test_same_value_without_label_is_not_guessed(tmp_path: Path) -> None:
    """同名值多处且没有版式标签时，不能随便取第一处。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 300, "NVD"),
            (400, 300, "NVD"),
        ],
        size=size,
    )

    locations = locate_fields(pdf, {1: 1}, {"declarecustom": "NVD"})

    assert "declarecustom" not in locations


def test_quote_prefers_same_column_anchor(tmp_path: Path) -> None:
    """同名值左右各一处时，用引用锚点所在栏，而不是只比距离。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (40, 280, "SHIPPER"),
            (40, 305, "LAX"),
            (380, 280, "AGENT"),
            (380, 305, "LAX"),
        ],
        size=size,
    )

    locations = locate_fields(
        pdf,
        {1: 1},
        {"sfg": "LAX"},
        quotes={"sfg": "AGENT LAX"},
    )

    x, _ = _box_origin(locations["sfg"][0]["bbox"], size)
    assert x > 300


def test_currency_uses_label_instead_of_fee_area(tmp_path: Path) -> None:
    """Currency 在页脚费用区也出现同名标签/值时，仍按币种栏标签定位。"""

    size = (595, 842)
    pdf = _build_pdf(
        tmp_path / "awb.pdf",
        [
            (290, 274, "Currency"),
            (292, 285, "USD"),
            (54, 712, "Currency"),
            (200, 353, "USD"),
        ],
        size=size,
    )

    locations = locate_fields(pdf, {1: 1}, {"currency": "USD"})

    x, _ = _box_origin(locations["currency"][0]["bbox"], size)
    assert x > 250
