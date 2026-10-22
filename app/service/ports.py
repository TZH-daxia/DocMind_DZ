"""港口字段口径：只接受 IATA 三字码。

单证上「Airport of Departure / Destination」常写机场全称或城市名；
能唯一确定港口时归一成三字码，确定不了就留空并把原文带回，交人工确认。
"""

from __future__ import annotations

import re

from app.service.normalizers import PORT_ALIASES, normalize_port

IATA_CODE_RE = re.compile(r"^[A-Z]{3}$")
PORT_FIELDS: tuple[str, ...] = ("sfg", "mdg")


def _substring_code(text: str) -> str | None:
    """在机场别名表里做包含匹配；只有唯一命中一个机场时才返回代码。"""

    normalized = re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", text.upper())
    if not normalized:
        return None
    matched: set[str] = set()
    for code, aliases in PORT_ALIASES.items():
        for alias in (code, *aliases):
            key = re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", alias.upper())
            # 太短的别名（如三字码本身）只做全等，避免误命中
            if len(key) < 4:
                continue
            if key in normalized:
                matched.add(code)
                break
    return next(iter(matched)) if len(matched) == 1 else None


def resolve_port(value: str | None) -> tuple[str | None, str]:
    """返回 (三字码, 无法确定时的原文)。

    - 原文本身就是三字码 → 原样返回；
    - 别名表能唯一命中（LOS ANGELES → LAX、SHANGHAI PUDONG… → PVG）→ 返回代码；
    - 只有城市/机场名且无法唯一确定（如 SHANGHAI）→ 返回 (None, 原文)。
    """

    text = (value or "").strip()
    if not text:
        return None, ""
    letters = re.sub(r"[^A-Za-z]", "", text).upper()
    if IATA_CODE_RE.fullmatch(letters):
        return letters, ""
    code = normalize_port(text)
    # normalize_port 会把任意独立的三字母单词当成代码（如 APT），
    # 只有出现在已知机场表里的代码才可信
    if IATA_CODE_RE.fullmatch(code) and code in PORT_ALIASES:
        return code, ""
    guess = _substring_code(text)
    if guess:
        return guess, ""
    return None, text


def normalize_port_fields(
    fields: dict[str, str | None],
    keys: tuple[str, ...] = PORT_FIELDS,
) -> dict[str, str]:
    """就地归一港口字段，返回 {字段: 原文}（只包含无法确定三字码的字段）。"""

    evidence: dict[str, str] = {}
    for key in keys:
        code, raw = resolve_port(fields.get(key))
        if raw:
            evidence[key] = raw
        fields[key] = code
    return evidence
