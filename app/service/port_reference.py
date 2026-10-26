"""港口三字码参考缓存：`data/reference_cache/hbinfo.json`。

参考 DocMind 的设计：三字码以缓存表为唯一事实来源，模型只负责给出
三字码或英文名，后端查表校验；查不到/多义时保留原文交人工，不猜。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CACHE_DIR = "reference_cache"
CACHE_FILE = "hbinfo.json"
# 单证英文名里的通用词，不参与词元匹配
NOISE_TOKENS = frozenset(
    {
        "AIRPORT",
        "INTERNATIONAL",
        "INTL",
        "APT",
        "AIR",
        "PORT",
        "TERMINAL",
        "CITY",
        "COUNTRY",
        "OF",
        "THE",
        "AND",
    }
)


def normalize_port_text(text: str) -> str:
    """去掉标点/空格并转大写；保留中文，便于中文名/英文名统一比较。"""

    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", text.upper())


class PortReferenceIndex:
    """hbinfo.json 的只读索引：三字码直通 → 整串 → 词元 → 前缀。"""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._by_code: dict[str, dict[str, Any]] = {}
        self._by_name: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            code = str(record.get("three_code") or "").strip().upper()
            if not code:
                continue
            name = normalize_port_text(str(record.get("english_name") or ""))
            self._by_code[code] = record
            if name:
                self._by_name.setdefault(name, []).append(record)

    @classmethod
    def load(cls, data_root: Path) -> PortReferenceIndex | None:
        path = Path(data_root) / CACHE_DIR / CACHE_FILE
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records") or []
        return cls(records) if records else None

    def lookup(self, value: str) -> tuple[str | None, str]:
        """返回 (唯一三字码, 匹配方式)；多义/未命中返回 (None, 原因)。"""

        text = (value or "").strip()
        if not text:
            return None, "empty"

        letters = re.sub(r"[^A-Za-z]", "", text).upper()
        if re.fullmatch(r"[A-Z]{3}", letters):
            if letters in self._by_code:
                return letters, "code"
            return None, "code_not_found"

        exact = self._by_name.get(normalize_port_text(text)) or []
        code = self._unique(exact)
        if code:
            return code, "name_exact"

        hits = self._token_hits(text) or self._prefix_hits(text)
        code = self._unique(hits)
        if code:
            return code, "name_token"
        return None, "ambiguous" if hits else "not_found"

    def search(self, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
        """给前端港口下拉用：按三字码/英文名做前缀与包含匹配。"""

        text = (keyword or "").strip().upper()
        if not text:
            return []
        normalized = normalize_port_text(text)
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for code, record in self._by_code.items():
            name = normalize_port_text(str(record.get("english_name") or ""))
            if code == text:
                rank = 0
            elif code.startswith(text):
                rank = 1
            elif name.startswith(normalized):
                rank = 2
            elif normalized and normalized in name:
                rank = 3
            elif normalized and normalized in code:
                rank = 4
            else:
                continue
            scored.append((rank, code, record))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [record for _, _, record in scored[:limit]]

    def _token_hits(self, text: str) -> list[dict[str, Any]]:
        tokens = [
            token
            for token in re.split(r"[^0-9A-Z]+", text.upper())
            if len(token) >= 3 and token not in NOISE_TOKENS
        ]
        if not tokens:
            return []
        scored: dict[str, tuple[int, dict[str, Any]]] = {}
        for name, records in self._by_name.items():
            score = sum(1 for token in tokens if token in name)
            if not score:
                continue
            for record in records:
                code = str(record.get("three_code") or "").upper()
                previous = scored.get(code)
                if previous is None or score > previous[0]:
                    scored[code] = (score, record)
        if not scored:
            return []
        best = max(score for score, _ in scored.values())
        return [record for score, record in scored.values() if score == best]

    def _prefix_hits(self, text: str) -> list[dict[str, Any]]:
        tokens = [
            token
            for token in re.split(r"[^0-9A-Z]+", text.upper())
            if len(token) >= 4 and token not in NOISE_TOKENS
        ]
        if not tokens:
            return []
        first = tokens[0]
        return [
            record
            for name, records in self._by_name.items()
            if name.startswith(first)
            for record in records
        ]

    @staticmethod
    def _unique(records: list[dict[str, Any]]) -> str | None:
        codes = {
            str(record.get("three_code") or "").upper()
            for record in records
            if record.get("three_code")
        }
        return next(iter(codes)) if len(codes) == 1 else None
