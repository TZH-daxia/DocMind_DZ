"""LLM 输出的 JSON 解析：容忍代码围栏、前后寒暄、尾随逗号。"""

from __future__ import annotations

import json
import re
from typing import Any


class JsonParseError(ValueError):
    pass


def _strip_fences(text: str) -> str:
    fenced = re.findall(r"```(?:json|JSON)?\s*(.*?)```", text, flags=re.DOTALL)
    return fenced[0].strip() if fenced else text.strip()


def _try_load(candidate: str) -> Any:
    candidate = candidate.strip().rstrip(",;")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # 去掉对象/数组内的尾随逗号
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
    return json.loads(repaired)


def parse_json(text: str, *, expected: type | tuple[type, ...] = (dict, list)) -> Any:
    """从模型回复里提取第一个合法 JSON 值。"""

    if not text or not text.strip():
        raise JsonParseError("模型返回为空")
    cleaned = _strip_fences(text)

    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for position in range(start, len(cleaned)):
                char = cleaned[position]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        candidate = cleaned[start : position + 1]
                        try:
                            value = _try_load(candidate)
                        except json.JSONDecodeError:
                            break
                        if isinstance(value, expected):
                            return value
                        break
            start = cleaned.find(opener, start + 1)
    raise JsonParseError("未找到合法 JSON")


def first_json_char(text: str) -> str:
    """返回回复中第一个 JSON 值的起始字符（`{` 或 `[`），用于选择解析方式。"""

    cleaned = _strip_fences(text or "")
    positions = [(index, char) for char in ("{", "[") if (index := cleaned.find(char)) >= 0]
    return min(positions)[1] if positions else ""


def parse_object(text: str) -> dict[str, Any]:
    value = parse_json(text, expected=dict)
    assert isinstance(value, dict)
    return value


def parse_array(text: str) -> list[Any]:
    value = parse_json(text, expected=list)
    assert isinstance(value, list)
    return value
