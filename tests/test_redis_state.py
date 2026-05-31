"""Tests for gateway/redis_state.py.

TDD step 3. Verifies:
- Lua scripts load and run via EVALSHA
- ratelimit script: atomic two-dim acquire, lazy refill keyed on ARGV[now_ms],
  rejection on either dim short, no partial deduction on rejection
- clamp script: shrinks remaining when observed < current
- probe lock: only one of N concurrent callers succeeds
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.redis_state import RedisState


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def state(redis):
    s = RedisState(redis)
    await s.load_scripts()
    return s


async def test_scripts_load(state: RedisState):
    scripts = await state.load_scripts()
    assert scripts.ratelimit and scripts.clamp


async def test_ratelimit_initial_acquire(state: RedisState):
    key = state.bucket_key("openai", "gpt-4o")
    ok, rpm_rem, tpm_rem = await state.ratelimit_acquire(
        key,
        now_ms=0,
        rpm_cap=10,
        tpm_cap=1000,
        refill_per_ms_rpm=10 / 60_000,
        refill_per_ms_tpm=1000 / 60_000,
        request_tokens=100,
    )
    assert ok is True
    assert rpm_rem == 9
    assert tpm_rem == 900


async def test_ratelimit_rejects_when_rpm_short(state: RedisState):
    key = state.bucket_key("openai", "gpt-4o")
    # Drain RPM by acquiring 10 times at the same now_ms (no refill).
    for _ in range(10):
        ok, _, _ = await state.ratelimit_acquire(
            key,
            now_ms=0,
            rpm_cap=10,
            tpm_cap=100_000,
            refill_per_ms_rpm=0.0,
            refill_per_ms_tpm=0.0,
            request_tokens=1,
        )
        assert ok
    ok, rpm_rem, tpm_rem = await state.ratelimit_acquire(
        key,
        now_ms=0,
        rpm_cap=10,
        tpm_cap=100_000,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=1,
    )
    assert ok is False
    assert rpm_rem == 0
    # TPM untouched on failure
    assert tpm_rem == 100_000 - 10


async def test_ratelimit_rejects_when_tpm_short(state: RedisState):
    key = state.bucket_key("openai", "gpt-4o")
    ok, _, _ = await state.ratelimit_acquire(
        key,
        now_ms=0,
        rpm_cap=1000,
        tpm_cap=500,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=400,
    )
    assert ok
    ok, rpm_rem, tpm_rem = await state.ratelimit_acquire(
        key,
        now_ms=0,
        rpm_cap=1000,
        tpm_cap=500,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=200,
    )
    assert ok is False
    # RPM untouched on TPM-rejection
    assert rpm_rem == 999
    assert tpm_rem == 100


async def test_ratelimit_lazy_refill(state: RedisState):
    key = state.bucket_key("openai", "gpt-4o")
    cap = 60
    refill = cap / 60_000  # full refill in 60s
    # Drain 50 at t=0
    for _ in range(50):
        ok, _, _ = await state.ratelimit_acquire(
            key,
            now_ms=0,
            rpm_cap=cap,
            tpm_cap=10_000_000,
            refill_per_ms_rpm=refill,
            refill_per_ms_tpm=1.0,
            request_tokens=1,
        )
        assert ok
    # Jump 30s -> should refill 30 RPM (capped at cap-current_consumed_so_far)
    ok, rpm_rem, _ = await state.ratelimit_acquire(
        key,
        now_ms=30_000,
        rpm_cap=cap,
        tpm_cap=10_000_000,
        refill_per_ms_rpm=refill,
        refill_per_ms_tpm=1.0,
        request_tokens=1,
    )
    assert ok
    # Remaining at t=30s: was 10, refilled +30 => 40, then -1 = 39
    assert rpm_rem == 39


async def test_clamp_shrinks_when_observed_lower(state: RedisState):
    key = state.bucket_key("openai", "gpt-4o")
    await state.ratelimit_acquire(
        key,
        now_ms=0,
        rpm_cap=1000,
        tpm_cap=100_000,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=10,
    )
    rpm, tpm = await state.ratelimit_clamp(key, rpm_observed=50, tpm_observed=5_000)
    assert rpm == 50
    assert tpm == 5_000


async def test_clamp_no_change_when_observed_higher(state: RedisState):
    key = state.bucket_key("openai", "gpt-4o")
    await state.ratelimit_acquire(
        key,
        now_ms=0,
        rpm_cap=100,
        tpm_cap=1000,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=10,
    )
    rpm, tpm = await state.ratelimit_clamp(key, rpm_observed=999, tpm_observed=99_999)
    # remained at 99 / 990, not raised
    assert rpm == 99
    assert tpm == 990


async def test_probe_lock_only_one_acquires(state: RedisState):
    probe = state.breaker_probe_key("openai", "gpt-4o")
    results = await asyncio.gather(
        *[state.acquire_probe_lock(probe, holder=f"h{i}", ttl_s=10) for i in range(8)]
    )
    assert sum(1 for r in results if r) == 1


async def test_probe_lock_expires(state: RedisState, redis):
    probe = state.breaker_probe_key("openai", "gpt-4o")
    assert await state.acquire_probe_lock(probe, holder="me", ttl_s=1)
    # Simulate expiry by deleting (fakeredis doesn't always honor TTL precisely
    # without time-travel; the test confirms we can re-acquire after key gone).
    await redis.delete(probe)
    assert await state.acquire_probe_lock(probe, holder="me2", ttl_s=10)
