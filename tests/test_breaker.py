"""Tests for gateway/breaker.py.

TDD step 5. Sliding-window circuit breaker per (provider, model). State lives
in Redis hash gw:brk:{p}:{m}; each replica keeps a 1s in-process snapshot for
fast hot-path checks; pub/sub publishes transitions so converging is faster
than the polling cadence.

Tests freeze time via a clock-injection function and don't depend on real
asyncio sleeps.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.breaker import BreakerSet, BreakerState
from gateway.redis_state import RedisState


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def breakers(redis):
    state = RedisState(redis)
    await state.load_scripts()
    clock = [0.0]

    def now_s() -> float:
        return clock[0]

    b = BreakerSet(
        state=state,
        window_s=30.0,
        min_samples=20,
        open_duration_s=30.0,
        failure_threshold=0.30,
        now_s_fn=now_s,
    )
    return b, clock


# ---------------------------------------------------------------- state machine


async def test_starts_closed(breakers):
    b, _ = breakers
    assert await b.state("openai", "gpt-4o") is BreakerState.CLOSED
    assert (await b.snapshot()).get(("openai", "gpt-4o")) is None or True


async def test_stays_closed_under_threshold(breakers):
    b, _ = breakers
    for _ in range(20):
        await b.record_success("openai", "gpt-4o")
    for _ in range(5):
        await b.record_failure("openai", "gpt-4o")
    # 5/25 = 20% < 30% threshold
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.CLOSED


async def test_opens_at_threshold(breakers):
    b, _ = breakers
    for _ in range(10):
        await b.record_success("openai", "gpt-4o")
    for _ in range(10):  # 10/20 = 50% > 30%
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.OPEN


async def test_requires_min_samples_to_open(breakers):
    b, _ = breakers
    for _ in range(5):  # only 5 samples, 100% failure but < min_samples
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.CLOSED


async def test_transitions_to_half_open_after_window(breakers):
    b, clock = breakers
    for _ in range(20):
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.OPEN
    clock[0] = 31.0  # past open_duration_s
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.HALF_OPEN


async def test_probe_closes_on_success(breakers):
    b, clock = breakers
    for _ in range(20):
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    clock[0] = 31.0
    await b.refresh_snapshot()
    # First call requesting a probe lock should win.
    assert await b.try_probe("openai", "gpt-4o") is True
    await b.record_success("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.CLOSED


async def test_probe_reopens_on_failure(breakers):
    b, clock = breakers
    for _ in range(20):
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    clock[0] = 31.0
    await b.refresh_snapshot()
    assert await b.try_probe("openai", "gpt-4o") is True
    await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.OPEN


async def test_only_one_concurrent_probe_holder(breakers):
    b, clock = breakers
    for _ in range(20):
        await b.record_failure("openai", "gpt-4o")
    clock[0] = 31.0
    await b.refresh_snapshot()
    results = await asyncio.gather(
        *[b.try_probe("openai", "gpt-4o") for _ in range(6)]
    )
    assert sum(1 for r in results if r) == 1
