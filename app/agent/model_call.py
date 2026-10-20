"""模型调用的墙钟超时、重试与多 provider 兜底。

openai SDK 的 timeout 只约束单次 HTTP，上游半开连接时会无限挂起，因此这里统一
用 `asyncio.wait_for` 加硬超时；失败按 provider 各自的预算重试，主 provider 整轮
耗尽后交给兜底 provider。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 1.0


class ModelCallFailedError(RuntimeError):
    def __init__(self, label: str, attempts: int, last_error: BaseException | None) -> None:
        super().__init__(f"{label} 连续 {attempts} 次调用失败：{last_error}")
        self.label = label
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class ProviderCall:
    provider: str
    label: str
    factory: Callable[[], Awaitable[Any]]
    timeout_seconds: float
    retries: int


async def call_with_retry(
    label: str,
    factory: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float,
    retries: int,
    should_retry: Callable[[Any], bool] | None = None,
) -> Any:
    attempts = max(1, retries + 1)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.wait_for(factory(), timeout=timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - 统一转成可重试失败
            last_error = exc
            logger.warning("%s 第 %s/%s 次调用失败：%s", label, attempt, attempts, exc)
        else:
            if should_retry is not None and should_retry(result):
                last_error = RuntimeError(f"{label} 返回内容不可用")
                logger.warning("%s 第 %s/%s 次返回内容不可用", label, attempt, attempts)
            else:
                return result
        if attempt < attempts:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise ModelCallFailedError(label, attempts, last_error) from last_error


async def call_with_failover(
    label: str,
    calls: Sequence[ProviderCall],
    *,
    should_retry: Callable[[Any], bool] | None = None,
) -> Any:
    if not calls:
        raise ValueError("至少需要一个 provider 调用")
    last_error: ModelCallFailedError | None = None
    for index, call in enumerate(calls):
        try:
            result = await call_with_retry(
                call.label,
                call.factory,
                timeout_seconds=call.timeout_seconds,
                retries=call.retries,
                should_retry=should_retry,
            )
        except ModelCallFailedError as exc:
            last_error = exc
            if index + 1 < len(calls):
                logger.warning("%s：主 provider 失败，切换兜底 provider", label)
            continue
        if index > 0:
            logger.warning("%s：本次由兜底 provider 完成", label)
        return result
    assert last_error is not None
    raise last_error
