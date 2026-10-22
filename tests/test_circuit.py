"""provider 熔断：阈值、冷却与兜底跳过。"""

import asyncio

import app.agent.circuit as circuit_module
from app.agent.circuit import CircuitBreaker
from app.agent.model_call import ProviderCall, call_with_failover


def test_breaker_opens_after_threshold():
    breaker = CircuitBreaker(fail_threshold=3, cooldown_seconds=90)
    breaker.record_failure("main")
    breaker.record_failure("main")
    assert breaker.is_open("main") is False
    breaker.record_failure("main")
    assert breaker.is_open("main") is True


def test_breaker_recovers_after_cooldown(monkeypatch):
    breaker = CircuitBreaker(fail_threshold=1, cooldown_seconds=10)
    breaker.record_failure("main")
    assert breaker.is_open("main") is True

    monkeypatch.setattr(circuit_module.time, "monotonic", lambda: 10**9)
    assert breaker.is_open("main") is False
    # 半开试探失败会重新熔断
    breaker.record_failure("main")
    assert breaker.is_open("main") is True


def test_breaker_success_resets_failures():
    breaker = CircuitBreaker(fail_threshold=2, cooldown_seconds=90)
    breaker.record_failure("main")
    breaker.record_success("main")
    breaker.record_failure("main")
    assert breaker.is_open("main") is False


async def test_failover_skips_provider_in_circuit():
    breaker = CircuitBreaker(fail_threshold=1, cooldown_seconds=90)
    breaker.record_failure("main")
    main_calls = 0

    async def main_factory() -> str:
        nonlocal main_calls
        main_calls += 1
        return "main"

    async def fallback_factory() -> str:
        return "fallback"

    calls = [
        ProviderCall(
            provider="main",
            label="main",
            factory=main_factory,
            timeout_seconds=0.1,
            retries=0,
        ),
        ProviderCall(
            provider="fallback",
            label="fallback",
            factory=fallback_factory,
            timeout_seconds=0.1,
            retries=0,
        ),
    ]
    result = await call_with_failover("circuit-test", calls, circuit=breaker)
    assert result == "fallback"
    assert main_calls == 0


async def test_failover_records_failure_into_circuit():
    breaker = CircuitBreaker(fail_threshold=1, cooldown_seconds=90)

    async def bad_factory() -> str:
        raise RuntimeError("boom")

    async def ok_factory() -> str:
        return "ok"

    calls = [
        ProviderCall(
            provider="main",
            label="main",
            factory=bad_factory,
            timeout_seconds=0.1,
            retries=0,
        ),
        ProviderCall(
            provider="fallback",
            label="fallback",
            factory=ok_factory,
            timeout_seconds=0.1,
            retries=0,
        ),
    ]
    assert await call_with_failover("circuit-fail", calls, circuit=breaker) == "ok"
    assert breaker.is_open("main") is True
    await asyncio.sleep(0)
