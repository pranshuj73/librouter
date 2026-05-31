"""Tests for gateway/routing/observe.py.

TDD step 6. The observe layer writes outcomes into per-second Redis hash
buckets (`gw:obs:{p}:{m}:{epoch_sec}`) used by the weight engine and refresh
loop to compute health scores.
"""

from __future__ import annotations

import pytest

from gateway.models import CandidateRef
from gateway.redis_state import RedisState
from gateway.routing.observe import Observer


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def observer(redis):
    state = RedisState(redis)
    await state.load_scripts()
    clock = [10.0]

    def now_s() -> float:
        return clock[0]

    return Observer(state=state, window_s=60, now_s_fn=now_s), clock, state


async def test_record_success_increments_bucket(observer):
    obs, _, state = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    await obs.record_success(cand, latency_s=0.5)
    key = state.observe_key("openai", "gpt-4o", 10)
    h = await state.client.hgetall(key)
    h = {k.decode(): v.decode() for k, v in h.items()}
    assert int(h["successes"]) == 1
    assert int(h["latency_sum_ms"]) == 500
    assert int(h["latency_count"]) == 1


async def test_record_failure_increments_with_kind(observer):
    obs, _, state = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    await obs.record_failure(cand, kind="RateLimited")
    key = state.observe_key("openai", "gpt-4o", 10)
    h = await state.client.hgetall(key)
    h = {k.decode(): v.decode() for k, v in h.items()}
    assert int(h["failures"]) == 1
    assert int(h["fail_RateLimited"]) == 1


async def test_records_have_ttl(observer):
    obs, _, state = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    await obs.record_success(cand, latency_s=0.2)
    key = state.observe_key("openai", "gpt-4o", 10)
    ttl = await state.client.ttl(key)
    assert ttl > 60  # buffered past window


async def test_aggregate_window(observer):
    obs, clock, _ = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    # Spread across several seconds
    clock[0] = 100.0
    await obs.record_success(cand, latency_s=0.1)
    clock[0] = 102.0
    await obs.record_success(cand, latency_s=0.2)
    clock[0] = 103.0
    await obs.record_failure(cand, kind="Timeout")
    clock[0] = 110.0
    agg = await obs.aggregate(cand)
    assert agg.successes == 2
    assert agg.failures == 1
    assert agg.total == 3
    assert agg.mean_latency_s == pytest.approx(0.15, rel=0.01)
    assert agg.error_rate == pytest.approx(1 / 3, rel=0.01)


async def test_aggregate_returns_zero_when_no_window(observer):
    obs, _, _ = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    agg = await obs.aggregate(cand)
    assert agg.total == 0
    assert agg.error_rate == 0.0
    assert agg.mean_latency_s == 0.0


# ---------------------------------------------------------------- §12 additions


async def test_record_failure_sets_ttl(observer):
    """Symmetric to `test_records_have_ttl` but for the failure path.

    `record_failure` also calls `expire(key, window_s * 4)`; without this
    test the failure-side TTL is untested (t-1 §12 Missing scenarios).
    """
    obs, _, state = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    await obs.record_failure(cand, kind="RateLimited")
    key = state.observe_key("openai", "gpt-4o", 10)
    ttl = await state.client.ttl(key)
    assert ttl > 60


async def test_aggregate_excludes_samples_outside_window(observer):
    """Samples older than `now - window_s` must not be summed.

    Record success at t=10; query at t=100 with window_s=60 (floor=40).
    The t=10 hash key is outside the [40, 100] inclusive range.
    """
    obs, clock, _ = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    clock[0] = 10.0
    await obs.record_success(cand, latency_s=0.5)
    clock[0] = 100.0
    agg = await obs.aggregate(cand)
    assert agg.total == 0
    assert agg.successes == 0
    assert agg.failures == 0


async def test_aggregate_failure_only_window_has_zero_mean_latency(observer):
    """Covers the `latency_count == 0` branch in observe.py:89.

    Only failures recorded => no latency samples => mean_latency_s == 0.0.
    """
    obs, _, _ = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    await obs.record_failure(cand, kind="Transient5xx")
    await obs.record_failure(cand, kind="Timeout")
    agg = await obs.aggregate(cand)
    assert agg.failures == 2
    assert agg.successes == 0
    assert agg.mean_latency_s == 0.0
    assert agg.error_rate == 1.0


async def test_multiple_failure_kinds_persist_independently(observer):
    """`fail_<Kind>` counters live alongside `failures` in the same hash."""
    obs, _, state = observer
    cand = CandidateRef(provider="openai", model="gpt-4o")
    await obs.record_failure(cand, kind="RateLimited")
    await obs.record_failure(cand, kind="Timeout")
    await obs.record_failure(cand, kind="Timeout")
    key = state.observe_key("openai", "gpt-4o", 10)
    h = await state.client.hgetall(key)
    h = {k.decode(): v.decode() for k, v in h.items()}
    assert int(h["fail_RateLimited"]) == 1
    assert int(h["fail_Timeout"]) == 2
    assert int(h["failures"]) == 3
