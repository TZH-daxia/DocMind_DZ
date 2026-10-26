"""港口字段口径：只接受 IATA 三字码。

单证上「Airport of Departure / Destination」常写机场全称或城市名；
能唯一确定港口时归一成三字码，确定不了就留空并把原文带回，交人工确认。
唯一事实来源是 `data/reference_cache/hbinfo.json`。
"""

from __future__ import annotations

from pathlib import Path

from app.service.port_reference import PortReferenceIndex

PORT_FIELDS: tuple[str, ...] = ("sfg", "mdg")

_INDEX_CACHE: dict[str, PortReferenceIndex | None] = {}


def _get_index() -> PortReferenceIndex | None:
    """惰性加载 data/reference_cache/hbinfo.json；缺失时港口一律待审核。

    按 data_root 分别缓存，避免测试/多实例之间互相污染。
    """

    from app.config import Settings

    try:
        data_root = str(Settings().data_root)
    except Exception:  # noqa: BLE001 - 配置异常时按无缓存处理
        return None
    if data_root not in _INDEX_CACHE:
        _INDEX_CACHE[data_root] = PortReferenceIndex.load(Path(data_root))
    return _INDEX_CACHE[data_root]


def resolve_port(
    value: str | None, index: PortReferenceIndex | None = None
) -> tuple[str | None, str]:
    """返回 (三字码, 无法确定时的原文)。

    只查缓存表：
    - 模型给的三字码在缓存表里存在 → 直接采用；
    - 城市/机场名唯一命中 → 归一成三字码；
    - 多义（如 SHANGHAI 对应 PVG/SHA）或表里查不到 → 返回 (None, 原文)。
    """

    text = (value or "").strip()
    if not text:
        return None, ""
    reference = index if index is not None else _get_index()
    if reference is None:
        return None, text
    code, _ = reference.lookup(text)
    return (code, "") if code else (None, text)


def normalize_port_fields(
    fields: dict[str, str | None],
    keys: tuple[str, ...] = PORT_FIELDS,
    index: PortReferenceIndex | None = None,
) -> dict[str, str]:
    """就地归一港口字段，返回 {字段: 原文}（只包含无法确定三字码的字段）。"""

    reference = index if index is not None else _get_index()
    evidence: dict[str, str] = {}
    for key in keys:
        code, raw = resolve_port(fields.get(key), index=reference)
        if raw:
            evidence[key] = raw
        fields[key] = code
    return evidence
