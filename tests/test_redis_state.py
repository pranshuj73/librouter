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


# Caveat per t-1 §8.5:
# fakeredis serializes commands on the event loop, so this verifies SET NX
# semantics under sequential atomic operations, NOT true concurrent contention.
# Real-Redis concurrency is covered separately in tests/test_app_e2e.py
# (TODO if not yet added).
async def test_probe_lock_only_one_acquires(state: RedisState):
    probe = state.breaker_probe_key("openai", "gpt-4o")
    results = await asyncio.gather(
        *[state.acquire_probe_lock(probe, holder=f"h{i}", ttl_s=10) for i in range(8)]
    )
    assert sum(1 for r in results if r) == 1


async def test_probe_lock_blocks_second_acquire_within_ttl(state: RedisState):
    """Per t-1 §8.6: under TTL the second acquire is blocked.

    This is what fakeredis can reliably test (the SET NX semantics).
    """
    probe = state.breaker_probe_key("openai", "gpt-4o")
    assert await state.acquire_probe_lock(probe, holder="me", ttl_s=10) is True
    assert await state.acquire_probe_lock(probe, holder="me2", ttl_s=10) is False


async def test_probe_lock_releases_after_explicit_delete(state: RedisState, redis):
    """After an explicit delete the lock can be re-acquired.

    True TTL-expiry semantics require real Redis; covered by integration tests.
    """
    probe = state.breaker_probe_key("openai", "gpt-4o")
    assert await state.acquire_probe_lock(probe, holder="me", ttl_s=10) is True
    await redis.delete(probe)
    assert await state.acquire_probe_lock(probe, holder="me2", ttl_s=10) is True


# ---------------------------------------------------------------- missing scenarios (t-1 §8)


async def test_noscript_fallback_to_eval(state: RedisState, redis):
    """Flushing the script cache forces _eval to catch NoScriptError and
    fall back to EVAL. Per t-1 §8: the NOSCRIPT fallback path."""
    await state.load_scripts()
    # Wipe the Redis script cache so EVALSHA will fail with NOSCRIPT.
    await redis.script_flush()

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


async def test_clamp_on_never_acquired_key_uses_observed(state: RedisState):
    """clamp Lua does `tonumber(h[1]) or rpm_observed` — on a fresh key the
    result equals the observed values. Per t-1 §8."""
    key = state.bucket_key("openai", "gpt-4o")
    rpm, tpm = await state.ratelimit_clamp(
        key, rpm_observed=42, tpm_observed=4242
    )
    assert rpm == 42
    assert tpm == 4242


async def test_per_key_isolation(state: RedisState):
    """Two distinct (provider, model) buckets do not share state. Per t-1 §8."""
    key_a = state.bucket_key("openai", "gpt-4o")
    key_b = state.bucket_key("anthropic", "haiku")

    # Drain bucket A completely.
    for _ in range(10):
        ok, _, _ = await state.ratelimit_acquire(
            key_a,
            now_ms=0,
            rpm_cap=10,
            tpm_cap=100_000,
            refill_per_ms_rpm=0.0,
            refill_per_ms_tpm=0.0,
            request_tokens=1,
        )
        assert ok
    # Now drained.
    ok, rpm_rem_a, _ = await state.ratelimit_acquire(
        key_a,
        now_ms=0,
        rpm_cap=10,
        tpm_cap=100_000,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=1,
    )
    assert ok is False
    assert rpm_rem_a == 0

    # Bucket B is unaffected — first acquire shows full cap minus 1.
    ok, rpm_rem_b, tpm_rem_b = await state.ratelimit_acquire(
        key_b,
        now_ms=0,
        rpm_cap=10,
        tpm_cap=100_000,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=1,
    )
    assert ok is True
    assert rpm_rem_b == 9
    assert tpm_rem_b == 100_000 - 1


async def test_request_tokens_zero_succeeds_and_reveals_state(state: RedisState):
    """request_tokens=0 acquire always succeeds and consumes 1 RPM (per Lua).
    Useful as a read-of-current-state. Per t-1 §8."""
    key = state.bucket_key("openai", "gpt-4o")
    # A few prior consumptions.
    for _ in range(3):
        ok, _, _ = await state.ratelimit_acquire(
            key,
            now_ms=0,
            rpm_cap=10,
            tpm_cap=1000,
            refill_per_ms_rpm=0.0,
            refill_per_ms_tpm=0.0,
            request_tokens=50,
        )
        assert ok
    # 3 RPM consumed, 150 TPM consumed -> remaining (7, 850).
    ok, rpm_rem, tpm_rem = await state.ratelimit_acquire(
        key,
        now_ms=0,
        rpm_cap=10,
        tpm_cap=1000,
        refill_per_ms_rpm=0.0,
        refill_per_ms_tpm=0.0,
        request_tokens=0,
    )
    assert ok is True
    # request_tokens=0 still consumes 1 RPM (per the Lua), so rpm goes 7 -> 6.
    assert rpm_rem == 6
    # TPM unchanged because request_tokens=0.
    assert tpm_rem == 850


async def test_refill_caps_at_capacity(state: RedisState):
    """After full drain, a 5-window clock jump must cap refill at rpm_cap
    (not 5x cap). Per t-1 §8."""
    key = state.bucket_key("openai", "gpt-4o")
    cap = 10
    refill = cap / 60_000  # full refill in 60s
    # Drain fully at t=0.
    for _ in range(cap):
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
    # Jump 5 minutes (5 windows). Refill is clamped to rpm_cap (10), not 50.
    ok, rpm_rem, _ = await state.ratelimit_acquire(
        key,
        now_ms=5 * 60_000,
        rpm_cap=cap,
        tpm_cap=10_000_000,
        refill_per_ms_rpm=refill,
        refill_per_ms_tpm=1.0,
        request_tokens=1,
    )
    assert ok is True
    assert rpm_rem == cap - 1


async def test_negative_clock_jump_does_not_refill(state: RedisState):
    """Per the Lua `if elapsed < 0 then elapsed = 0 end`: a backwards clock
    jump must not produce spurious refill. Per t-1 §8."""
    key = state.bucket_key("openai", "gpt-4o")
    cap = 10
    refill = cap / 60_000
    # Acquire at now_ms=10000.
    ok, rpm_rem_1, _ = await state.ratelimit_acquire(
        key,
        now_ms=10_000,
        rpm_cap=cap,
        tpm_cap=10_000_000,
        refill_per_ms_rpm=refill,
        refill_per_ms_tpm=1.0,
        request_tokens=1,
    )
    assert ok is True
    assert rpm_rem_1 == cap - 1

    # Acquire at now_ms=5000 (backwards). elapsed clamped to 0 — no refill.
    ok, rpm_rem_2, _ = await state.ratelimit_acquire(
        key,
        now_ms=5_000,
        rpm_cap=cap,
        tpm_cap=10_000_000,
        refill_per_ms_rpm=refill,
        refill_per_ms_tpm=1.0,
        request_tokens=1,
    )
    assert ok is True
    assert rpm_rem_2 == cap - 2
