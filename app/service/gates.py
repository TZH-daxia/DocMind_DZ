"""进程内并发闸门：按名字惰性创建 `asyncio.Semaphore`。

设计参考 DocMind：
- 信号量必须在事件循环内创建，因此不在服务构造函数里建，而是首次使用时按名字创建；
- 超限一律排队（await），不拒绝请求，也没有队列长度上限；
- 闸门只包住"真正占用资源"的那一段：排队等待、重试退避都不占槽位。

进程模型约束：并发控制只在单进程内有效，部署时必须单副本、单 uvicorn worker。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ConcurrencyGates:
    """按名字维护的信号量集合，同时记录排队人数供事件文案使用。"""

    def __init__(self) -> None:
        self._gates: dict[str, asyncio.Semaphore] = {}
        self._waiting: dict[str, int] = {}

    def semaphore(self, name: str, limit: int) -> asyncio.Semaphore:
        gate = self._gates.get(name)
        if gate is None:
            gate = self._gates[name] = asyncio.Semaphore(max(1, limit))
        return gate

    def waiting(self, name: str) -> int:
        """当前正在排队等该闸门的数量（不含已持有槽位的）。"""

        return self._waiting.get(name, 0)

    @asynccontextmanager
    async def acquire(self, name: str, limit: int) -> AsyncIterator[int]:
        """进入闸门，yield 排队时前面的人数（0 表示直接拿到槽位）。"""

        gate = self.semaphore(name, limit)
        ahead = self._waiting.get(name, 0)
        self._waiting[name] = ahead + 1
        try:
            await gate.acquire()
        finally:
            self._waiting[name] = max(0, self._waiting.get(name, 1) - 1)
        try:
            yield ahead
        finally:
            gate.release()
