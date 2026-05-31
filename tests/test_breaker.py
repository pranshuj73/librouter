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
    # Per t-1 §4.1: the breaker has no snapshot entry until samples are
    # recorded. Verify that explicitly (no `or True` tautology).
    snap = await b.snapshot()
    assert ("openai", "gpt-4o") not in snap


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


# Caveat per t-1 §4.5:
# fakeredis serializes commands on the event loop, so this verifies SET NX
# semantics under sequential atomic operations, NOT true concurrent contention.
# Real-Redis concurrency is covered separately in tests/test_app_e2e.py
# (TODO if not yet added).
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


# ---------------------------------------------------------------- missing scenarios (t-1 §4)


async def test_breakers_are_independent_per_candidate(breakers):
    """Tripping one (provider, model) must not affect another. Per t-1 §4."""
    b, _ = breakers
    for _ in range(20):
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.OPEN
    # anthropic/haiku has never been touched — still CLOSED.
    assert await b.state("anthropic", "haiku") is BreakerState.CLOSED


async def test_state_for_unseen_candidate_returns_closed(breakers):
    """A fresh BreakerSet with no samples returns CLOSED for any candidate."""
    b, _ = breakers
    assert await b.state("never", "seen") is BreakerState.CLOSED


async def test_open_to_half_open_to_open_cycle(breakers):
    """OPEN -> HALF_OPEN -> OPEN; second open updates opened_at_s. Per t-1 §4."""
    b, clock = breakers
    # First trip.
    for _ in range(20):
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.OPEN
    first_open_at = (await b.snapshot())[("openai", "gpt-4o")].opened_at_s

    # Advance clock past open_duration_s -> HALF_OPEN.
    clock[0] = 31.0
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.HALF_OPEN

    # Acquire probe and record failure -> OPEN again.
    assert await b.try_probe("openai", "gpt-4o") is True
    clock[0] = 32.0
    await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.OPEN

    second_open_at = (await b.snapshot())[("openai", "gpt-4o")].opened_at_s
    assert second_open_at > first_open_at


async def test_probe_lock_ttl_blocks_concurrent_then_releases(breakers):
    """Second try_probe under TTL returns False; after explicit delete a
    third try_probe succeeds.

    Note: fakeredis doesn't reliably honor TTL expiry without time-travel,
    so we simulate expiry by deleting the key.
    """
    b, clock = breakers
    # Trip and transition to HALF_OPEN to make probing legitimate.
    for _ in range(20):
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    clock[0] = 31.0
    await b.refresh_snapshot()

    # First probe wins.
    assert await b.try_probe("openai", "gpt-4o") is True
    # Second under TTL is blocked.
    assert await b.try_probe("openai", "gpt-4o") is False

    # Simulate expiry via explicit delete (fakeredis TTL isn't reliable).
    probe_key = b._state.breaker_probe_key("openai", "gpt-4o")
    await b._state.client.delete(probe_key)

    # Third probe now succeeds.
    assert await b.try_probe("openai", "gpt-4o") is True


async def test_threshold_boundary_at_exactly_minimum_samples(breakers):
    """At exactly min_samples with failures/total == failure_threshold,
    code uses `>=` so the breaker should trip. Per gateway/breaker.py:244."""
    b, _ = breakers
    # 14 successes + 6 failures = 20 samples, 6/20 = 0.30 == threshold.
    for _ in range(14):
        await b.record_success("openai", "gpt-4o")
    for _ in range(6):
        await b.record_failure("openai", "gpt-4o")
    await b.refresh_snapshot()
    assert await b.state("openai", "gpt-4o") is BreakerState.OPEN


async def test_refresh_snapshot_swaps_to_new_dict(breakers):
    """refresh_snapshot rebuilds a fresh dict and atomically swaps _snapshot.

    After a call to refresh_snapshot the resulting _snapshot object must be a
    *different* dict instance than the one that was there before the call.
    (#6.1 — atomic-swap prevents torn reads from concurrent async paths.)
    """
    b, _ = breakers
    await b.record_success("openai", "gpt-4o")
    initial_snapshot_ref = b._snapshot  # capture identity before refresh
    await b.refresh_snapshot()
    # The swap must have replaced the dict — different object identity.
    assert b._snapshot is not initial_snapshot_ref
