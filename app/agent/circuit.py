"""模型 provider 熔断器（参考 DocMind 的实现）。

- 同一 provider 连续失败达到阈值后进入熔断期，期间直接跳过、走兜底 provider；
- 冷却时间结束后放行一次试探，成功则清零，失败则重新计时；
- 只按 provider 名称计状态，进程内有效（多副本需各自熔断）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreaker:
    fail_threshold: int = 3
    cooldown_seconds: float = 90.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def is_open(self, provider: str) -> bool:
        opened_at = self._opened_at.get(provider)
        if opened_at is None:
            return False
        if time.monotonic() - opened_at >= self.cooldown_seconds:
            # 冷却结束：放行一次试探，结果回来后再决定是否继续熔断
            self._opened_at.pop(provider, None)
            self._failures[provider] = 0
            logger.info("provider %s 熔断冷却结束，放行试探调用", provider)
            return False
        return True

    def record_failure(self, provider: str) -> None:
        count = self._failures.get(provider, 0) + 1
        self._failures[provider] = count
        if count >= self.fail_threshold and provider not in self._opened_at:
            self._opened_at[provider] = time.monotonic()
            logger.warning(
                "provider %s 连续失败 %s 次，进入熔断期 %.0f 秒",
                provider,
                count,
                self.cooldown_seconds,
            )

    def record_success(self, provider: str) -> None:
        self._failures.pop(provider, None)
        self._opened_at.pop(provider, None)
