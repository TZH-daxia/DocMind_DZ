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

# 版式栏位名 → 分单字段；用于把搜索限制在该栏附近
FIELD_LABEL_HINTS: dict[str, tuple[str, ...]] = {
    "hawb": ("HAWB NO", "HOUSE AIR WAYBILL NO"),
    # 申报价值取 Declared Value for Customs（不是 for Carriage）
    "declarecustom": ("DECLARED VALUE FOR CUSTOMS",),
    "sfg": ("AIRPORT OF DEPARTURE",),
    "mdg": ("AIRPORT OF DESTINATION",),
    "hbh": ("REQUESTED FLIGHT/DATE", "REQUESTED FLIGHT"),
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
    ]


def _alignment_score(label: Any, rect: Any) -> float:
    """越小越可信：值与标签水平对齐、且紧挨在标签下方。"""

    if rect.intersects(label):
        return float("inf")
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


def _pick_city_rect(page: Any, hits: list[Any]) -> Any:
    """城市值通常与邮编/国家同一行（地址块最后一行），优先选这种命中。"""

    try:
        words = page.get_text("words")
    except Exception:  # noqa: BLE001 - 页面解析异常时退回第一处
        return hits[0]
    for rect in hits:
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
    return hits[0]


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


def _locate_one(page: Any, key: str, value: str) -> list[Any]:
    """在单页上定位一个字段，返回最多 MAX_BOXES 个矩形。"""

    queries = _query_variants(value) + _date_variants(value)
    if not queries:
        return []
    hits: list[Any] = []
    if key.startswith("companycode_"):
        hits = _exact_word_hits(page, value)
    for query in queries:
        if hits:
            break
        rects = page.search_for(query)
        if rects:
            hits = rects
            break
    if not hits:
        return []
    if key == "hbrq" or key.endswith("_date"):
        hits = _merge_date_hits(page, hits)

    # 城市没有可靠的表头锚点（"City" 可能出现在别处），用「同行有邮编/国家」判定
    if key.endswith("_city") or key.startswith("city_"):
        return [_pick_city_rect(page, hits)]

    labels: list[Any] = []
    for hint in FIELD_LABEL_HINTS.get(key, ()):
        labels.extend(page.search_for(hint))
    if not labels:
        # 没有版式标签可锚定（如地址、品名）：只能取第一处命中
        return hits[:MAX_BOXES]

    scored = []
    for rect in hits:
        score = min(_alignment_score(label, rect) for label in labels)
        if score < MAX_LABEL_DISTANCE:
            scored.append((score, rect))
    if not scored:
        # 标签附近找不到可信位置：宁可不框，也不要框错
        return []
    scored.sort(key=lambda item: item[0])
    return [scored[0][1]]


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
    try:
        with pymupdf.open(pdf_path) as pdf:
            for key, value in fields.items():
                if not value or not str(value).strip():
                    continue
                for global_index, page_no in page_map.items():
                    if page_no < 1 or page_no > pdf.page_count:
                        continue
                    page = pdf.load_page(page_no - 1)
                    rects = _locate_one(page, key, str(value))
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
                    break
    except Exception as exc:  # noqa: BLE001 - 定位失败不影响抽取结果
        logger.warning("字段定位失败（%s）：%s", pdf_path.name, exc)
        return {}
    return locations
