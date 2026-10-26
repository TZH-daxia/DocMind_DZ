"""字段值归一化：用于分单 vs 舱单核对。

同一份数据在分单和舱单上的书写方式经常不同（CA 1234 / CA1234、2026/10/20 /
20OCT2026、上海浦东 / PVG、+86 138... / 138...），核对前必须归一化，否则会产生
大量假差异。归一化只做「改写」，不做「推断」：无法归一的值保持原文，交回核对层
按原文比较。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

_CN_DATE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")
_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        [
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ],
        start=1,
    )
}


def basic_normalize(value: str | None) -> str:
    """全角转半角 + 去空白 + 大写（保留中文与字母数字）。"""

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    return normalized.upper()


def normalize_text(value: str | None) -> str:
    """名称/地址比对用：去掉空白与常见标点，仅保留字母数字与中文。"""

    normalized = basic_normalize(value)
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", normalized)


# 地址末尾的国家写法差异不应算不一致（US / UNITED STATES / USA …）
COUNTRY_SUFFIXES: dict[str, str] = {
    "UNITEDSTATESOFAMERICA": "US",
    "UNITEDSTATES": "US",
    "UNITEDSTATE": "US",
    "USA": "US",
    "PEOPLESREPUBLICOFCHINA": "CN",
    "PRCHINA": "CN",
    "CHINA": "CN",
    "HONGKONG": "HK",
    "MEXICO": "MX",
    "JAPAN": "JP",
    "KOREA": "KR",
    "SOUTHKOREA": "KR",
    "GERMANY": "DE",
    "FRANCE": "FR",
    "NETHERLANDS": "NL",
    "UNITEDKINGDOM": "GB",
    "UK": "GB",
    "VIETNAM": "VN",
    "THAILAND": "TH",
    "SINGAPORE": "SG",
    "MALAYSIA": "MY",
    "INDIA": "IN",
    "AUSTRALIA": "AU",
    "CANADA": "CA",
    "BRAZIL": "BR",
    "UAE": "AE",
    "UNITEDARABEMIRATES": "AE",
}

_COUNTRY_ORDER = sorted(COUNTRY_SUFFIXES, key=len, reverse=True)


def normalize_address(value: str | None) -> str:
    """地址归一化：先去掉空白标点，再把末尾国家名统一成两位代码。"""

    normalized = normalize_text(value)
    if not normalized:
        return ""
    for alias in _COUNTRY_ORDER:
        if normalized.endswith(alias):
            prefix = normalized[: -len(alias)]
            return f"{prefix}{COUNTRY_SUFFIXES[alias]}"
    return normalized


def similarity(left: str | None, right: str | None) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def similarity_address(left: str | None, right: str | None) -> float:
    left_norm = normalize_address(left)
    right_norm = normalize_address(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def normalize_flight_no(value: str | None) -> str:
    normalized = basic_normalize(value)
    normalized = re.sub(r"[\s\-_/]+", "", normalized)
    return normalized


def normalize_doc_no(value: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", basic_normalize(value))


def normalize_email(value: str | None) -> str:
    normalized = basic_normalize(value).lower()
    return re.sub(r"^MAILTO:", "", normalized).strip()


def normalize_phone(value: str | None) -> str:
    """只保留数字；去掉国际前缀 0086/86。"""

    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    return digits


def normalize_number(value: str | None) -> float | None:
    """抽取第一个数字；支持千分位、货币符号、负号、括号负数。"""

    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return -abs(number) if negative else number


def normalize_int(value: str | None) -> int | None:
    number = normalize_number(value)
    return None if number is None else int(round(number))


def normalize_date(value: str | None) -> str:
    """尽量转成 YYYY-MM-DD；无法识别时返回原文。"""

    normalized = basic_normalize(value)
    if not normalized:
        return ""
    match = _CN_DATE.search(normalized)
    if match:
        return _build_date(match.group(1), match.group(2), match.group(3)) or normalized
    match = _COMPACT_DATE.match(normalized)
    if match:
        return _build_date(match.group(1), match.group(2), match.group(3)) or normalized

    cleaned = normalized.replace(",", " ").replace(".", " ")
    tokens = [token for token in re.split(r"[\s/\-]+", cleaned) if token]
    # 20OCT2026 / 20OCT26
    for token in tokens:
        match = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2,4})", token)
        if match and match.group(2) in _MONTH_NAMES:
            return (
                _build_date(match.group(3), _MONTH_NAMES[match.group(2)], match.group(1))
                or normalized
            )
    # 20 OCT 2026
    for index in range(len(tokens) - 2):
        day, month, year = tokens[index], tokens[index + 1], tokens[index + 2]
        if day.isdigit() and month in _MONTH_NAMES and year.isdigit():
            return _build_date(year, _MONTH_NAMES[month], day) or normalized
    # 2026-10-20 / 2026/10/20
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", normalized)
    if match:
        return _build_date(match.group(1), match.group(2), match.group(3)) or normalized
    # 20/10/2026（按日/月/年，无法区分时保留原文含义）
    match = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", normalized)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), match.group(3)
        day, month = (first, second) if first > 12 else (second, first)
        return _build_date(year, month, day) or normalized
    return normalized


def _build_date(year: str | int, month: str | int, day: str | int) -> str | None:
    try:
        year_int = int(year)
        if year_int < 100:
            year_int += 2000 if year_int < 70 else 1900
        return date(year_int, int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return None



