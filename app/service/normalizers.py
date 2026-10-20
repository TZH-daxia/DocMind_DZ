"""字段值归一化：用于分单 vs 舱单核对。

同一份数据在分单和舱单上的书写方式经常不同（CA 1234 / CA1234、2026/10/20 /
20OCT2026、上海浦东 / PVG、+86 138... / 138...），核对前必须归一化，否则会产生
大量假差异。归一化只做「改写」，不做「推断」：无法归一的值保持原文，交回核对层
按原文比较。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher

# 常见机场/城市别名（代码 → 别名）。可在此继续扩展，命中不到的保持原文比较。
PORT_ALIASES: dict[str, tuple[str, ...]] = {
    "PVG": ("上海浦东", "浦东", "上海浦东国际机场", "SHANGHAI PUDONG"),
    "SHA": ("上海虹桥", "虹桥", "上海虹桥国际机场", "SHANGHAI HONGQIAO"),
    "PEK": ("北京首都", "首都机场", "北京首都国际机场", "BEIJING CAPITAL"),
    "PKX": ("北京大兴", "大兴机场", "北京大兴国际机场", "BEIJING DAXING"),
    "CAN": ("广州", "广州白云", "白云机场", "GUANGZHOU"),
    "SZX": ("深圳", "深圳宝安", "宝安机场", "SHENZHEN"),
    "CTU": ("成都双流", "双流机场", "CHENGDU SHUANGLIU"),
    "TFU": ("成都天府", "天府机场", "CHENGDU TIANFU"),
    "CKG": ("重庆", "重庆江北", "江北机场", "CHONGQING"),
    "HGH": ("杭州", "杭州萧山", "萧山机场", "HANGZHOU"),
    "NKG": ("南京", "南京禄口", "禄口机场", "NANJING"),
    "XMN": ("厦门", "厦门高崎", "高崎机场", "XIAMEN"),
    "TAO": ("青岛", "青岛胶东", "胶东机场", "QINGDAO"),
    "TSN": ("天津", "天津滨海", "滨海机场", "TIANJIN"),
    "WUH": ("武汉", "武汉天河", "天河机场", "WUHAN"),
    "XIY": ("西安", "西安咸阳", "咸阳机场", "XIAN"),
    "KMG": ("昆明", "昆明长水", "长水机场", "KUNMING"),
    "CGO": ("郑州", "郑州新郑", "新郑机场", "ZHENGZHOU"),
    "CSX": ("长沙", "长沙黄花", "黄花机场", "CHANGSHA"),
    "HAK": ("海口", "海口美兰", "美兰机场", "HAIKOU"),
    "SYX": ("三亚", "三亚凤凰", "凤凰机场", "SANYA"),
    "DLC": ("大连", "大连周水子", "周水子机场", "DALIAN"),
    "SHE": ("沈阳", "沈阳桃仙", "桃仙机场", "SHENYANG"),
    "HRB": ("哈尔滨", "哈尔滨太平", "太平机场", "HARBIN"),
    "URC": ("乌鲁木齐", "乌鲁木齐地窝堡", "地窝堡机场", "URUMQI"),
    "NGB": ("宁波", "宁波栎社", "栎社机场", "NINGBO"),
    "WNZ": ("温州", "温州龙湾", "龙湾机场", "WENZHOU"),
    "HFE": ("合肥", "合肥新桥", "新桥机场", "HEFEI"),
    "FOC": ("福州", "福州长乐", "长乐机场", "FUZHOU"),
    "TNA": ("济南", "济南遥墙", "遥墙机场", "JINAN"),
    "ZUH": ("珠海", "珠海金湾", "金湾机场", "ZHUHAI"),
    "HKG": ("香港", "香港国际机场", "HONG KONG"),
    "MFM": ("澳门", "澳门国际机场", "MACAU"),
    "TPE": ("台北桃园", "桃园机场", "TAIPEI"),
    "KHH": ("高雄", "高雄小港", "KAOHSIUNG"),
    "LAX": ("洛杉矶", "洛杉矶国际机场", "LOS ANGELES"),
    "JFK": ("纽约", "纽约肯尼迪", "肯尼迪机场", "NEW YORK"),
    "ORD": ("芝加哥", "芝加哥奥黑尔", "奥黑尔机场", "CHICAGO"),
    "SFO": ("旧金山", "圣弗朗西斯科", "SAN FRANCISCO"),
    "SEA": ("西雅图", "SEATTLE"),
    "MIA": ("迈阿密", "MIAMI"),
    "DFW": ("达拉斯", "DALLAS"),
    "ATL": ("亚特兰大", "ATLANTA"),
    "BOS": ("波士顿", "BOSTON"),
    "IAD": ("华盛顿", "WASHINGTON"),
    "YVR": ("温哥华", "VANCOUVER"),
    "YYZ": ("多伦多", "TORONTO"),
    "MEX": ("墨西哥城", "MEXICO CITY"),
    "GRU": ("圣保罗", "SAO PAULO"),
    "LHR": ("伦敦", "伦敦希思罗", "LONDON"),
    "CDG": ("巴黎", "巴黎戴高乐", "PARIS"),
    "AMS": ("阿姆斯特丹", "AMSTERDAM"),
    "FRA": ("法兰克福", "FRANKFURT"),
    "MUC": ("慕尼黑", "MUNICH"),
    "MAD": ("马德里", "MADRID"),
    "BCN": ("巴塞罗那", "BARCELONA"),
    "FCO": ("罗马", "ROME"),
    "MXP": ("米兰", "MILAN"),
    "ZRH": ("苏黎世", "ZURICH"),
    "VIE": ("维也纳", "VIENNA"),
    "CPH": ("哥本哈根", "COPENHAGEN"),
    "ARN": ("斯德哥尔摩", "STOCKHOLM"),
    "OSL": ("奥斯陆", "OSLO"),
    "HEL": ("赫尔辛基", "HELSINKI"),
    "BRU": ("布鲁塞尔", "BRUSSELS"),
    "IST": ("伊斯坦布尔", "ISTANBUL"),
    "DXB": ("迪拜", "DUBAI"),
    "DOH": ("多哈", "DOHA"),
    "AUH": ("阿布扎比", "ABU DHABI"),
    "RUH": ("利雅得", "RIYADH"),
    "CAI": ("开罗", "CAIRO"),
    "JNB": ("约翰内斯堡", "JOHANNESBURG"),
    "NBO": ("内罗毕", "NAIROBI"),
    "SIN": ("新加坡", "樟宜", "SINGAPORE"),
    "KUL": ("吉隆坡", "KUALA LUMPUR"),
    "BKK": ("曼谷", "BANGKOK"),
    "CGK": ("雅加达", "JAKARTA"),
    "MNL": ("马尼拉", "MANILA"),
    "HAN": ("河内", "HANOI"),
    "SGN": ("胡志明", "胡志明市", "HO CHI MINH"),
    "PNH": ("金边", "PHNOM PENH"),
    "DEL": ("德里", "新德里", "DELHI"),
    "BOM": ("孟买", "MUMBAI"),
    "BLR": ("班加罗尔", "BANGALORE"),
    "SYD": ("悉尼", "SYDNEY"),
    "MEL": ("墨尔本", "MELBOURNE"),
    "BNE": ("布里斯班", "BRISBANE"),
    "PER": ("珀斯", "PERTH"),
    "AKL": ("奥克兰", "AUCKLAND"),
    "NRT": ("东京成田", "成田机场", "TOKYO NARITA"),
    "HND": ("东京羽田", "羽田机场", "TOKYO HANEDA"),
    "KIX": ("大阪", "关西机场", "OSAKA"),
    "NGO": ("名古屋", "NAGOYA"),
    "ICN": ("首尔仁川", "仁川机场", "SEOUL INCHEON"),
    "GMP": ("首尔金浦", "金浦机场", "SEOUL GIMPO"),
    "PUS": ("釜山", "BUSAN"),
    "CTS": ("札幌", "新千岁", "SAPPORO"),
    "FUK": ("福冈", "FUKUOKA"),
}

_PORT_LOOKUP: dict[str, str] = {}
for _code, _aliases in PORT_ALIASES.items():
    for _alias in (_code, *_aliases):
        _PORT_LOOKUP[re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", _alias.upper())] = _code

PORT_NAME_SUFFIXES = (
    "国际机场",
    "机场",
    "航空港",
    "航站",
    "市",
    "站",
    "AIRPORT",
    "INTL",
    "INTERNATIONAL",
)

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


def normalize_port(value: str | None) -> str:
    """港口归一化：优先返回 IATA 三字码，否则返回去后缀的规范文本。"""

    normalized = basic_normalize(value)
    if not normalized:
        return ""
    # 形如 "SHANGHAI (PVG)" / "PVG-浦东"：先抓三字码
    code_match = re.search(r"(?<![A-Z0-9])([A-Z]{3})(?![A-Z0-9])", normalized)
    compact = re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", normalized)
    if compact in _PORT_LOOKUP:
        return _PORT_LOOKUP[compact]
    # 去掉括号内容后再试一次
    without_parentheses = re.sub(r"[（(].*?[)）]", "", normalized).strip()
    compact_without_parentheses = re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", without_parentheses)
    if compact_without_parentheses in _PORT_LOOKUP:
        return _PORT_LOOKUP[compact_without_parentheses]
    for suffix in PORT_NAME_SUFFIXES:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            trimmed = compact[: -len(suffix)]
            if trimmed in _PORT_LOOKUP:
                return _PORT_LOOKUP[trimmed]
            compact = trimmed
    if compact in _PORT_LOOKUP:
        return _PORT_LOOKUP[compact]
    if code_match:
        return code_match.group(1)
    return compact


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


def humanize_timestamp(value: datetime | None) -> str:
    return value.isoformat() if value else ""
