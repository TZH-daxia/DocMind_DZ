"""字段定位：在 PDF 文本层里找到字段值，返回页码与归一化坐标框。

定位策略（按优先级）：
1. **标签锚定**：先搜版式栏位名（如 `AIRPORT OF DEPARTURE`、`GROSS WEIGHT`），
   再在标签附近挑与它水平对齐、垂直最近的命中。这样能区分：
   - 同一值在页面多处出现（PVG 在航线栏和目的港栏）；
   - 两个不同字段值相同（Gross Weight 与 Chargeable Weight 都是 440）；
   - 短值（"1"、"Q"）不会命中页脚文字。
2. **字段值变体**：原文、压缩空白、日期改写（2026-09-30 → 30SEP / 30 SEP 2026 等）。
3. **找不到可信位置就不高亮**：宁可没有框，也不要把光标移到错误的栏位。

坐标统一归一化到 0~1（相对页面宽高），前端按百分比定位，缩放时无需重算。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 同一字段只标一处：值在页面上重复出现时不再散开多个框
MAX_BOXES = 1
MAX_QUERY_CHARS = 40
MIN_TOKEN_LEN = 3
# 标签与值的最大可接受距离（PDF 点，页面高约 842）：超过视为「不是同一栏」
MAX_LABEL_DISTANCE = 260.0
# 值命中与原文引用锚点的最大距离：超过视为不是同一处，回退到标签逻辑
QUOTE_ANCHOR_DISTANCE = 300.0
# 参与人栏位锚点：先定位公司名/地址，供同栏城市等字段判断左右栏
PARTY_ANCHOR_PREFIXES = ("companytitle_", "address_")

# 国家二字码 → 单证上常见的全称写法（单证多印全称，抽取结果却是编码）
COUNTRY_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "CN": ("CHINA", "P.R. CHINA", "PRC"),
    "HK": ("HONG KONG",),
    "TW": ("TAIWAN",),
    "MO": ("MACAO", "MACAU"),
    "US": ("UNITED STATES", "USA", "U.S.A."),
    "MX": ("MEXICO",),
    "CA": ("CANADA",),
    "BR": ("BRAZIL",),
    "DE": ("GERMANY",),
    "GB": ("UNITED KINGDOM", "ENGLAND", "UK"),
    "FR": ("FRANCE",),
    "IT": ("ITALY",),
    "ES": ("SPAIN",),
    "NL": ("NETHERLANDS",),
    "BE": ("BELGIUM",),
    "PL": ("POLAND",),
    "SE": ("SWEDEN",),
    "CH": ("SWITZERLAND",),
    "AT": ("AUSTRIA",),
    "CZ": ("CZECH REPUBLIC", "CZECH"),
    "RU": ("RUSSIA",),
    "TR": ("TURKEY",),
    "AE": ("UNITED ARAB EMIRATES", "UAE"),
    "SA": ("SAUDI ARABIA",),
    "IN": ("INDIA",),
    "VN": ("VIETNAM",),
    "TH": ("THAILAND",),
    "MY": ("MALAYSIA",),
    "SG": ("SINGAPORE",),
    "ID": ("INDONESIA",),
    "PH": ("PHILIPPINES",),
    "JP": ("JAPAN",),
    "KR": ("SOUTH KOREA", "KOREA"),
    "AU": ("AUSTRALIA",),
    "NZ": ("NEW ZEALAND",),
}

# 版式栏位名 → 分单字段；用于把搜索限制在该栏附近
FIELD_LABEL_HINTS: dict[str, tuple[str, ...]] = {
    "hawb": ("HAWB NO", "HOUSE AIR WAYBILL NO"),
    # 申报价值取 Declared Value for Customs（不是 for Carriage）
    "declarecustom": ("DECLARED VALUE FOR CUSTOMS",),
    "sfg": ("AIRPORT OF DEPARTURE",),
    "mdg": ("AIRPORT OF DESTINATION",),
    "hbh": ("REQUESTED FLIGHT/DATE", "REQUESTED FLIGHT"),
    "currency": ("CURRENCY",),
    "hbrq": ("REQUESTED FLIGHT/DATE", "REQUESTED FLIGHT"),
    # 分单上的 "No. Of Pieces RCP" 常被拆成三行，整串搜也能跨行命中；
    # 只留较短的标签，避免版式变化后一个都匹配不上
    "pieces": ("NO. OF PIECES", "PIECES"),
    "zdpiece": ("NO. OF PIECES RCP", "PIECES RCP"),
    "zdweight": ("GROSS WEIGHT",),
    "ratelevel": ("RATE CLASS",),
    "jfweight": ("CHARGEABLE WEIGHT",),
    "rate": ("RATE/CHARGE", "RATE CHARGE"),
    "zdvolume": ("VOLUME",),
    "pm": ("NATURE AND QUANTITY",),
    "companytitle_fhr_hawb": ("SHIPPER'S NAME",),
    "address_fhr_hawb": ("SHIPPER'S NAME AND ADDRESS",),
    "companycode_fhr_hawb": ("SHIPPER'S NAME AND ADDRESS",),
    "companytitle_shr_hawb": ("CONSIGNEE'S NAME",),
    "address_shr_hawb": ("CONSIGNEE'S NAME AND ADDRESS",),
    "companycode_shr_hawb": ("CONSIGNEE'S NAME AND ADDRESS",),
}


def _normalize_box(
    rect: tuple[float, float, float, float], width: float, height: float
) -> list[float]:
    x0, y0, x1, y1 = rect
    if width <= 0 or height <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        round(max(0.0, x0 / width), 4),
        round(max(0.0, y0 / height), 4),
        round(max(0.0, (x1 - x0) / width), 4),
        round(max(0.0, (y1 - y0) / height), 4),
    ]


def _query_variants(value: str) -> list[str]:
    """由字段值生成检索词：原文、压缩空白、截断、前两个词。"""

    text = value.strip()
    if not text:
        return []
    variants: list[str] = []
    for candidate in (
        text,
        re.sub(r"\s+", " ", text),
        text[:MAX_QUERY_CHARS] if len(text) > MAX_QUERY_CHARS else "",
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)
    tokens = [
        token
        for token in re.split(r"[\s,，、/]+", text)
        if len(token) >= MIN_TOKEN_LEN
    ]
    if len(tokens) >= 2:
        joined = " ".join(tokens[:2])
        if joined not in variants:
            variants.append(joined)
    return variants


def _date_variants(value: str) -> list[str]:
    """把 ISO 日期改写成单证上常见的写法（30SEP / 30 SEP 2026 等）。"""

    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value.strip())
    if not match:
        return []
    try:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return []
    month = parsed.strftime("%b").upper()
    return [
        f"{parsed.day:02d}{month}",  # 30SEP
        f"{parsed.day:02d} {month}",  # 30 SEP
        f"{parsed.day:02d}{month}{parsed.strftime('%y')}",  # 30SEP26
        f"{parsed.day:02d}{month}{parsed.year}",  # 30SEP2026
        f"{parsed.day:02d}/{parsed.month:02d}/{parsed.year}",
        f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year}",
        # 航班日期常只印“日”（如 `5Y518/22`），最后才退回两位日；靠版式标签锚定
        f"{parsed.day:02d}",
    ]


def _same_column(rect: Any, anchor: Any, page_width: float) -> bool:
    """两个矩形是否在同一栏：水平有重叠，或中心 x 足够接近。"""

    overlap = min(rect.x1, anchor.x1) - max(rect.x0, anchor.x0)
    if overlap > 0:
        return True
    rect_center = (rect.x0 + rect.x1) / 2
    anchor_center = (anchor.x0 + anchor.x1) / 2
    return abs(rect_center - anchor_center) <= page_width * 0.15


def _alignment_score(label: Any, rect: Any) -> float:
    """越小越可信：值与标签水平对齐、且紧挨在标签下方。

    值与标签框重叠在紧凑版式里很常见（小字号、标签紧贴值），不能直接判死；
    只按中心距离打分，重叠时垂直距离记 0。
    """

    label_center = (label.x0 + label.x1) / 2
    rect_center = (rect.x0 + rect.x1) / 2
    horizontal = abs(label_center - rect_center)
    if rect.y1 <= label.y0:  # 值在标签上方
        vertical = (label.y0 - rect.y1) * 1.6
    elif rect.y0 >= label.y1:  # 值在标签下方（常态）
        vertical = rect.y0 - label.y1
    else:  # 垂直重叠
        vertical = 0.0
    return horizontal + vertical * 1.5


def _pick_city_rect(
    page: Any, hits: list[Any], anchor: dict[str, float] | None = None
) -> Any:
    """城市值优先选「同一栏 + 紧邻地址块」的命中。

    AWB 同一栏里城市名会出现多次（发货人地址、代理地址、页脚签章），
    只按“同行有邮编”选会指到代理栏；用同栏地址的定位结果做锚点：
    先取地址块正下方同列缩进的命中，再退回水平最近。
    """

    candidates = hits
    if anchor:
        x0 = anchor.get("x0", 0.0)
        y1 = anchor.get("y1", 0.0)
        block = [
            rect
            for rect in hits
            if abs(rect.x0 - x0) <= 60 and y1 - 5 <= rect.y0 <= y1 + 120
        ]
        if block:
            candidates = block
        else:
            center = anchor.get("xc", x0)
            near = [
                rect for rect in hits if abs((rect.x0 + rect.x1) / 2 - center) <= 150
            ]
            if near:
                candidates = near
    try:
        words = page.get_text("words")
    except Exception:  # noqa: BLE001 - 页面解析异常时退回第一处
        return candidates[0]
    for rect in candidates:
        # 只认同一行、且紧跟在城市右侧的词（邮编/国家），避免把上方或下方行算进来
        same_row = [
            word
            for word in words
            if abs(word[1] - rect.y0) < 1.5 and 0 <= word[0] - rect.x0 < 160
        ]
        if any(re.fullmatch(r"\d{4,6}", str(word[4])) for word in same_row):
            return rect
        if any(
            str(word[4]).upper() in {"CN", "US", "USA", "CHINA", "UNITED"}
            for word in same_row
        ):
            return rect
    if anchor:
        center = anchor.get("xc", anchor.get("x0", 0.0))
        return min(
            candidates, key=lambda rect: abs((rect.x0 + rect.x1) / 2 - center)
        )
    return candidates[0]


def _merge_date_hits(page: Any, hits: list[Any]) -> list[Any]:
    """把同一行被拆开的日期片段合并成一个框。

    单证上的日期常和航班号连排（`N8652/30` + `SEP/26`），`search_for("30 SEP")`
    会返回两个单字矩形；只取其中一个，高亮就停在月份上，看起来像少了「日」。
    """

    import pymupdf

    rows: list[list[Any]] = []
    for rect in sorted(hits, key=lambda item: (item.y0, item.x0)):
        if rows and abs(rows[-1][0].y0 - rect.y0) < 2:
            rows[-1].append(rect)
        else:
            rows.append([rect])

    try:
        words = page.get_text("words")
    except Exception:  # noqa: BLE001 - 取不到词时只合并命中框
        words = []

    merged: list[Any] = []
    for row in rows:
        x0 = min(rect.x0 for rect in row)
        y0 = min(rect.y0 for rect in row)
        x1 = max(rect.x1 for rect in row)
        y1 = max(rect.y1 for rect in row)
        # 月份常和年份连写在一个词里（SEP/26），补上该词的剩余部分
        for word in words:
            if x0 <= word[0] <= x1 and abs(word[1] - y0) < 2:
                x1 = max(x1, word[2])
        merged.append(pymupdf.Rect(x0, y0, x1, y1))
    return merged


def _exact_word_hits(page: Any, value: str) -> list[Any]:
    """只认与值完全相同的词（不分大小写）。

    国家二字码这类 2 字符值按子串搜会命中页首条码 `CN1HBLPVGDFW` 里的 `CN`，
    必须整词匹配才算数。
    """

    probe = value.strip().upper()
    if not probe:
        return []
    try:
        import pymupdf

        return [
            pymupdf.Rect(word[:4])
            for word in page.get_text("words")
            if str(word[4]).strip().upper() == probe
        ]
    except Exception:  # noqa: BLE001 - 取不到词表时退回子串搜索
        return []


def _plain_quote(text: str) -> str:
    """把 VLM 输出的 Markdown 引用还原成可在 PDF 文本层检索的纯文本。"""

    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[|*#`>]+", " ", text)
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def _quote_segments(text: str) -> list[str]:
    """把引用切成较长的片段，用于整条匹配失败时找唯一锚点。"""

    plain = _plain_quote(text)
    segments = [
        item.strip(" ,;:()[]{}")
        for item in re.split(r"[\s,;:()|/]+", plain)
    ]
    # 太短的片段容易多命中，按长度降序只保留较长且有区分度的
    unique = {item for item in segments if 6 <= len(item) <= 40}
    return sorted(unique, key=len, reverse=True)[:8]


def _box_distance(rect: Any, anchor: Any) -> float:
    """两个矩形的中心距离。"""

    rect_x = (rect.x0 + rect.x1) / 2
    rect_y = (rect.y0 + rect.y1) / 2
    anchor_x = (anchor.x0 + anchor.x1) / 2
    anchor_y = (anchor.y0 + anchor.y1) / 2
    return ((rect_x - anchor_x) ** 2 + (rect_y - anchor_y) ** 2) ** 0.5


def _quote_boxes(page: Any, quote: str | None) -> list[Any]:
    """用原文引用在页面上找锚点：整条 → 去 Markdown → 唯一片段。"""

    if not quote or not quote.strip():
        return []
    for query in (quote.strip(), _plain_quote(quote)):
        if len(query) < 3:
            continue
        try:
            rects = page.search_for(query)
        except Exception:  # noqa: BLE001 - 单条引用检索失败不影响其他候选
            rects = []
        if rects:
            return rects
    # 整条匹配不上（标签与值跨块、换行差异）：用能唯一定位的片段作锚点
    for segment in _quote_segments(quote):
        try:
            rects = page.search_for(segment)
        except Exception:  # noqa: BLE001
            continue
        if len(rects) == 1:
            return rects
    return []


def _prefix_hits(page: Any, value: str) -> list[Any]:
    """长值（品名/地址）在页面被换行拆开时，用可命中的最长前缀兜底。

    例：模型输出 `..._BRK_ASM_ HS CODE`，页面实际是 `..._BRK` 换行 `_ASM_`，
    整串与所有变体都搜不到；截到 `..._BRK` 仍能指示位置。
    """

    text = value.strip()
    if len(text) < 12:
        return []
    step = max(4, len(text) // 20)
    for size in range(len(text) - 1, 7, -step):
        rects = page.search_for(text[:size])
        if rects:
            return rects
    return []


def _country_hits(page: Any, value: str) -> list[Any]:
    """国家栏：整词匹配编码，同时补上该编码对应的国家全称（单证多印全称）。"""

    hits = _exact_word_hits(page, value)
    probe = value.strip().upper()
    for name in COUNTRY_NAME_ALIASES.get(probe, ()):
        try:
            hits.extend(page.search_for(name))
        except Exception:  # noqa: BLE001 - 单个名称搜索失败不影响其他候选
            continue
    return hits


def _locate_one(
    page: Any,
    key: str,
    value: str,
    anchor: dict[str, float] | None = None,
    quote_boxes: list[Any] | None = None,
) -> list[Any]:
    """在单页上定位一个字段，返回最多 MAX_BOXES 个矩形。"""

    queries = _query_variants(value) + _date_variants(value)
    if not queries:
        return []
    hits: list[Any] = []
    if key.startswith("companycode_"):
        hits = _country_hits(page, value)
    for query in queries:
        if hits:
            break
        rects = page.search_for(query)
        if rects:
            hits = rects
            break
    if not hits:
        # 整串/变体都搜不到时，品名这类长值可能被换行拆开，用最长前缀兜底
        hits = _prefix_hits(page, value)
    if not hits:
        return []
    if key == "hbrq" or key.endswith("_date"):
        hits = _merge_date_hits(page, hits)

    width = float(page.rect.width)

    # 有原文引用时优先在引用附近取值：比版式标签更能区分同名值的不同栏位。
    # 但要求值与引用锚点在同一栏/同一行，左右栏的同名值不能仅凭距离入选。
    if quote_boxes:
        anchor_box = quote_boxes[0]
        candidates = [
            rect
            for rect in hits
            if _same_column(rect, anchor_box, width)
            and _row_distance(anchor_box, rect) <= 160
        ]
        if candidates:
            return [min(candidates, key=lambda rect: _box_distance(rect, anchor_box))]
        # 引用与所有命中都不同栏：不要用引用强行选，继续按版式标签定位

    # 城市没有可靠的表头锚点（"City" 可能出现在别处），用「同行有邮编/国家」判定
    if key.endswith("_city") or key.startswith("city_"):
        return [_pick_city_rect(page, hits, anchor)]

    labels: list[Any] = []
    for hint in FIELD_LABEL_HINTS.get(key, ()):
        labels.extend(page.search_for(hint))
    if not labels:
        # 没有版式标签：唯一命中可以框；同名值多处时不随便取第一处，
        # 有引用锚点时退而取离锚点最近的一处，否则不框
        if len(hits) == 1:
            return hits
        if quote_boxes:
            return [min(hits, key=lambda rect: _box_distance(rect, quote_boxes[0]))]
        return []

    scored = []
    for rect in hits:
        best_label = min(labels, key=lambda label: _alignment_score(label, rect))
        score = _alignment_score(best_label, rect)
        # 必须与标签同栏（或同一行），否则同名值会被选到隔壁栏
        if score < MAX_LABEL_DISTANCE and (
            _same_column(rect, best_label, width)
            or _row_distance(best_label, rect) <= 24
        ):
            scored.append((score, rect))
    if not scored:
        # 标签附近找不到可信位置：宁可不框，也不要框错
        return []
    scored.sort(key=lambda item: item[0])
    return [scored[0][1]]


def _block_containing(page: Any, rect: Any) -> Any:
    """返回包含给定矩形的文本块 bbox（标签所在栏位）。"""

    import pymupdf

    try:
        blocks = page.get_text("blocks")
    except Exception:  # noqa: BLE001 - 取不到块时退回 None
        return None
    for block in blocks:
        box = pymupdf.Rect(block[:4])
        if box.width <= rect.width or box.height <= rect.height:
            continue
        if box.contains(rect):
            return box
    return None


def _join_words(words: list[Any]) -> str:
    """把词按行拼接成一行文本（同 y 为一组，组内按 x 排序）。"""

    rows: dict[int, list[Any]] = {}
    for word in words:
        rows.setdefault(round(float(word[1]) / 2), []).append(word)
    lines: list[str] = []
    for key in sorted(rows):
        ordered = sorted(rows[key], key=lambda item: item[0])
        line = " ".join(str(item[4]) for item in ordered).strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def _card_candidates(
    page: Any, value: str, card_top: float, card_bottom: float
) -> list[Any]:
    """卡片范围内的候选矩形：先整词匹配，找不到再退回子串匹配。

    件数这类短值（"1"、"4"）按子串搜索会命中同一卡片里的邮编 215300、
    EIN 43-134239000，所以优先只认与值完全相同的整词；整词没有命中时
    （如目的港 DFW 印成 USDFW）才退回子串搜索。
    """

    probe = value.strip()
    if not probe:
        return []
    exact: list[Any] = []
    try:
        import pymupdf

        exact = [
            pymupdf.Rect(word[:4])
            for word in page.get_text("words")
            if str(word[4]).strip() == probe and card_top <= word[1] <= card_bottom
        ]
    except Exception:  # noqa: BLE001 - 解析异常时退回子串匹配
        exact = []
    if exact:
        return exact
    return [
        rect for rect in page.search_for(probe) if card_top <= rect.y0 <= card_bottom
    ]


def _row_distance(anchor: Any, rect: Any) -> float:
    """值到锚点行的纵向距离：同一行算 0，越远越大。"""

    if rect.y0 >= anchor.y1:
        return rect.y0 - anchor.y1
    if rect.y1 <= anchor.y0:
        return anchor.y0 - rect.y1
    return 0.0


def _card_rect(
    page: Any, anchor: Any, value: str, card_top: float, card_bottom: float
) -> Any | None:
    """在锚点卡片范围内找值：优先与锚点同行，其次纵向最近。

    舱单可能是「一行一条记录」的表格，也可能是 SHIPMENT DETAILS 卡片；
    限制在锚点（分单号）所在卡片内搜索，避免命中页眉航线、其他卡片。
    不能取 `search_for` 的第一处命中：它按文本流（栏位）顺序返回，
    与版面上下位置无关，会把件数定位到地址列里的数字。
    """

    candidates = _card_candidates(page, value, card_top, card_bottom)
    if not candidates:
        return None
    return min(candidates, key=lambda rect: (_row_distance(anchor, rect), rect.x0))


def locate_manifest_fields(
    pdf_path: Path,
    page_map: dict[int, int],
    fields: dict[str, str | None],
    anchor: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """舱单行/卡片定位：用分单号锚定，再在锚点所在卡片范围内找其他字段值。

    舱单同一值可能多处出现（如 DFW 同时出现在航线栏和目的地栏），
    先找到本条记录的 HAWB 号（唯一），再把搜索限制在该卡片范围内。
    """

    if not page_map or not fields:
        return {}
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        return {}

    anchor_value = str(anchor or fields.get("hawb_no") or "").strip()
    if not anchor_value:
        return locate_fields(pdf_path, page_map, fields)

    try:
        with pymupdf.open(pdf_path) as pdf:
            for global_index, page_no in page_map.items():
                if page_no < 1 or page_no > pdf.page_count:
                    continue
                page = pdf.load_page(page_no - 1)
                hits = page.search_for(anchor_value)
                if not hits:
                    continue
                anchor_rect = hits[0]
                card_top = anchor_rect.y0 - 5
                card_bottom = float(page.rect.height)
                # 同页还有其他分单号命中时，以最近的一个作为本卡片的下边界；
                # 距离太近（<40pt）的命中是同一卡片里的重复标题，不算新卡片
                for other in hits[1:]:
                    if other.y0 > card_top + 40:
                        card_bottom = min(card_bottom, other.y0 - 5)
                        break
                width = float(page.rect.width)
                height = float(page.rect.height)
                locations: dict[str, list[dict[str, Any]]] = {}
                for key, value in fields.items():
                    text = str(value or "").strip()
                    if not text:
                        continue
                    if key == "hawb_no":
                        rect = anchor_rect
                    else:
                        rect = _card_rect(
                            page, anchor_rect, text, card_top, card_bottom
                        )
                    if rect is None:
                        continue
                    locations[key] = [
                        {
                            "page": global_index,
                            "bbox": _normalize_box(
                                (rect.x0, rect.y0, rect.x1, rect.y1), width, height
                            ),
                        }
                    ]
                return locations
    except Exception as exc:  # noqa: BLE001
        logger.warning("舱单字段定位失败（%s）：%s", pdf_path.name, exc)
    return {}


def locate_fields(
    pdf_path: Path,
    page_map: dict[int, int],
    fields: dict[str, str | None],
    quotes: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """在 PDF 中定位字段值。

    Args:
        pdf_path: 该分单对应的 PDF（原件或 Office 转换后的 PDF）。
        page_map: 全局页 index → PDF 页号（1 起）。
        fields: 字段 key → 值。

    Returns:
        字段 key → `[{"page": 全局页 index, "bbox": [x, y, w, h]}]`；找不到的字段不出现。
    """

    if not page_map or not fields:
        return {}
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - 依赖缺失
        return {}

    locations: dict[str, list[dict[str, Any]]] = {}
    # 收发货人两栏常出现同名城市：先定位公司名/地址，作为同栏锚点
    ordered = sorted(
        fields.items(),
        key=lambda item: 0 if item[0].startswith(PARTY_ANCHOR_PREFIXES) else 1,
    )
    anchors: dict[str, dict[str, float]] = {}
    try:
        with pymupdf.open(pdf_path) as pdf:
            for key, value in ordered:
                if not value or not str(value).strip():
                    continue
                party = key.split("_", 1)[1] if "_" in key else ""
                rects: list[Any] = []
                for global_index, page_no in page_map.items():
                    if page_no < 1 or page_no > pdf.page_count:
                        continue
                    page = pdf.load_page(page_no - 1)
                    quote_boxes = _quote_boxes(page, (quotes or {}).get(key))
                    rects = _locate_one(
                        page,
                        key,
                        str(value),
                        anchor=anchors.get(party),
                        quote_boxes=quote_boxes,
                    )
                    if not rects and len(quote_boxes) == 1:
                        # 值本身定位不到（被改写/换行异常）时，退化为引用片段的位置
                        rects = quote_boxes
                    if not rects:
                        continue
                    width = float(page.rect.width)
                    height = float(page.rect.height)
                    locations[key] = [
                        {
                            "page": global_index,
                            "bbox": _normalize_box(
                                (rect.x0, rect.y0, rect.x1, rect.y1), width, height
                            ),
                        }
                        for rect in rects[:MAX_BOXES]
                    ]
                    if key.startswith(PARTY_ANCHOR_PREFIXES):
                        first = rects[0]
                        # 地址块比公司名更能代表这一栏的范围（城市在其正下方）
                        if key.startswith("address_") or party not in anchors:
                            anchors[party] = {
                                "x0": first.x0,
                                "xc": (first.x0 + first.x1) / 2,
                                "y1": first.y1,
                            }
                    break
    except Exception as exc:  # noqa: BLE001 - 定位失败不影响抽取结果
        logger.warning("字段定位失败（%s）：%s", pdf_path.name, exc)
        return {}
    return locations


def read_handling_info(
    pdf_path: Path, page_map: dict[int, int]
) -> tuple[str | None, dict[str, Any] | None]:
    """按版面读取 Handling Information 栏正文。

    模型对这一段经常漏抽（填 null），这里从标签所在文本块里取标签下方的文字，
    多行拼成一行。返回 (文本, {"page", "bbox"})；bbox 用于判断联系方式是否取自该栏。
    """

    if not page_map:
        return None, None
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - 依赖缺失
        return None, None

    try:
        with pymupdf.open(pdf_path) as pdf:
            for global_index, page_no in page_map.items():
                if page_no < 1 or page_no > pdf.page_count:
                    continue
                page = pdf.load_page(page_no - 1)
                labels = page.search_for("HANDLING INFORMATION")
                if not labels:
                    continue
                label = labels[0]
                block = _block_containing(page, label)
                if block is None:
                    continue
                # 标签常压在正文中间（AWB 的 Handling Information 栏标题与内容
                # 垂直重叠），以标签顶为界取本栏内容，同时排除标签自身的词。
                # 长行会溢出栏位右边线，只限制词的起点，不限制终点。
                top = label.y0 - 10
                words = []
                for word in page.get_text("words"):
                    if word[1] < top or word[3] > block.y1 + 2:
                        continue
                    if not block.x0 - 2 <= word[0] <= block.x1 + 2:
                        continue
                    rect = pymupdf.Rect(word[:4])
                    # 只排除标签自身的短词；正文长行会横跨标签区域，不能整行丢掉
                    if rect.width <= label.width * 1.5 and rect.intersects(label):
                        continue
                    words.append(word)
                text = _join_words(words)
                if not text:
                    continue
                width = float(page.rect.width)
                height = float(page.rect.height)
                return text, {
                    "page": global_index,
                    "bbox": _normalize_box(
                        (block.x0, top, block.x1, block.y1), width, height
                    ),
                }
    except Exception as exc:  # noqa: BLE001 - 兜底失败不影响主流程
        logger.warning("Handling Information 读取失败（%s）：%s", pdf_path.name, exc)
    return None, None


def read_value_below_label(
    pdf_path: Path,
    page_map: dict[int, int],
    label: str,
    max_gap: float = 80.0,
) -> str | None:
    """读取版式标签正下方（同列、最近一行）的第一个词。

    用于抽取值与版式栏位对不上时按页面纠正（如 Airport of Departure 栏的 LAX）。
    """

    if not page_map:
        return None
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - 依赖缺失
        return None

    try:
        with pymupdf.open(pdf_path) as pdf:
            for page_no in page_map.values():
                if page_no < 1 or page_no > pdf.page_count:
                    continue
                page = pdf.load_page(page_no - 1)
                labels = page.search_for(label)
                if not labels:
                    continue
                anchor = labels[0]
                rows: dict[int, list[Any]] = {}
                for word in page.get_text("words"):
                    if word[3] < anchor.y1 - 1 or word[1] > anchor.y1 + max_gap:
                        continue
                    if word[0] < anchor.x0 - 12 or word[0] > anchor.x1 + 40:
                        continue
                    rows.setdefault(round(float(word[1]) / 2), []).append(word)
                if not rows:
                    continue
                # 标签下面的续行（"(Addr. of First Carrier)..."）和相邻栏位标签
                # 都不是值；港口栏只认三字码，逐行找到第一个为止
                for key in sorted(rows):
                    ordered = sorted(rows[key], key=lambda item: item[0])
                    for word in ordered:
                        text = str(word[4]).strip()
                        if re.fullmatch(r"[A-Z]{3}", text):
                            return text
    except Exception as exc:  # noqa: BLE001 - 兜底失败不影响主流程
        logger.warning("栏位取值失败（%s / %s）：%s", pdf_path.name, label, exc)
    return None
