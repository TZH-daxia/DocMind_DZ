"""并发闸门：上限、排队计数与排队不占超时。"""

import asyncio

from app.agent.model_call import call_with_retry
from app.service.gates import ConcurrencyGates


async def test_gate_limits_concurrency():
    gates = ConcurrencyGates()
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with gates.acquire("demo", 2):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak == 2


async def test_waiting_count_reports_queued():
    gates = ConcurrencyGates()
    release = asyncio.Event()

    async def holder() -> None:
        async with gates.acquire("demo", 1):
            await release.wait()

    async def queued() -> int:
        async with gates.acquire("demo", 1) as ahead:
            return ahead

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    queued_task = asyncio.create_task(queued())
    await asyncio.sleep(0.01)
    # holder 已持有槽位，queued 正在等：等待计数为 1
    assert gates.waiting("demo") == 1

    release.set()
    ahead = await queued_task
    assert ahead == 0
    await holder_task
    assert gates.waiting("demo") == 0


async def test_gate_waiting_is_not_counted_in_timeout():
    """排队等槽位的时间不应计入模型调用超时。"""

    gate = asyncio.Semaphore(1)

    async def hold() -> None:
        async with gate:
            await asyncio.sleep(0.2)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.01)

    async def factory() -> str:
        return "ok"

    result = await call_with_retry(
        "guard-test",
        factory,
        timeout_seconds=0.05,
        retries=0,
        guard=lambda: gate,
    )
    assert result == "ok"
    await holder
