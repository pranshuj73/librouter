"""Tests for gateway/ratelimit.py.

TDD step 4. RedisTokenBucket wraps redis_state.ratelimit_acquire with a clean
API: per (provider, model) capacity from config, deterministic clock via an
injected `clock_ms_fn`, plus `clamp` for vendor-header-driven shrinkage.
"""

from __future__ import annotations

import pytest

from gateway.models import RateLimitEntry
from gateway.ratelimit import RedisTokenBucket, estimate_tokens
from gateway.redis_state import RedisState


pytestmark = pytest.mark.asyncio


def _est_tokens(_provider: str, _model: str) -> int:
    return 100  # fixed-cost stub for these tests


@pytest.fixture
async def bucket(redis):
    state = RedisState(redis)
    await state.load_scripts()
    clock = [0]

    def now_ms() -> int:
        return clock[0]

    limits = {
        "openai/gpt-4o": RateLimitEntry(rpm=10, tpm=1000),
        "anthropic/haiku": RateLimitEntry(rpm=60, tpm=60_000),
    }
    rb = RedisTokenBucket(state=state, limits=limits, now_ms_fn=now_ms)
    return rb, clock


async def test_initial_acquire(bucket):
    rb, _ = bucket
    ok, rpm, tpm = await rb.try_acquire("openai", "gpt-4o", request_tokens=100)
    assert ok is True
    assert rpm == 9
    assert tpm == 900


async def test_blocks_when_rpm_drained(bucket):
    rb, _ = bucket
    for _ in range(10):
        assert (await rb.try_acquire("openai", "gpt-4o", request_tokens=1))[0]
    ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
    assert ok is False


async def test_blocks_when_tpm_short(bucket):
    rb, _ = bucket
    ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=900)
    assert ok
    # only 100 TPM left, request for 200 should fail
    ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=200)
    assert ok is False


async def test_refills_after_time_passes(bucket):
    rb, clock = bucket
    for _ in range(10):
        ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
        assert ok
    # Empty now
    ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
    assert ok is False
    # Jump 60s — full refill window
    clock[0] = 60_000
    ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
    assert ok is True


async def test_clamp_reduces_remaining(bucket):
    rb, _ = bucket
    await rb.try_acquire("anthropic", "haiku", request_tokens=1)
    new_rpm, new_tpm = await rb.clamp(
        "anthropic", "haiku", rpm_observed=5, tpm_observed=500
    )
    assert new_rpm == 5
    assert new_tpm == 500

    # No-op direction: observed values WAY above current — clamp must not raise
    # them (it only shrinks). The bucket should stay at the freshly-clamped
    # state (rpm=5, tpm=500).
    same_rpm, same_tpm = await rb.clamp(
        "anthropic", "haiku", rpm_observed=10_000, tpm_observed=10_000_000
    )
    assert same_rpm == 5
    assert same_tpm == 500


async def test_unknown_provider_model_raises(bucket):
    rb, _ = bucket
    with pytest.raises(KeyError):
        await rb.try_acquire("nope", "missing", request_tokens=1)


# ---------------------------------------------------------------- estimate_tokens
# Pure function; no fixtures needed. Locks in the bucket-sizing contract that
# drives both TPM acquisition and routing weight. Per t-1 §7 Missing scenarios.


@pytest.mark.parametrize(
    "prompt_chars,max_tokens,expected",
    [
        (0, 0, 1),           # floor: max(1, ...)
        (1, 0, 1),           # 1 // 4 == 0; still hits floor
        (0, 100, 100),       # only response budget
        (400, 100, 200),     # 400 // 4 == 100; + 100
        (40_000, 1_000, 11_000),
    ],
)
async def test_estimate_tokens(prompt_chars, max_tokens, expected):
    # async def to compose with module-level pytest.mark.asyncio; the function
    # is pure and never awaits.
    assert estimate_tokens(prompt_chars, max_tokens) == expected


# ---------------------------------------------------------------- contract / isolation


async def test_remaining_consumes_one_rpm_documents_contract(bucket):
    """`remaining()` consumes 1 RPM as a documented side effect.

    The function docstring (`gateway/ratelimit.py`) explicitly notes that the
    underlying acquire-with-0-tokens trick consumes 1 RPM. This test locks in
    that contract so anyone "fixing" the side effect knows they've broken an
    expectation (or chooses to remove the contract explicitly).
    """
    rb, _ = bucket
    # Fresh bucket: 10 RPM cap.
    ok, rpm_after_acquire, _ = await rb.try_acquire(
        "openai", "gpt-4o", request_tokens=1
    )
    assert ok is True
    assert rpm_after_acquire == 9  # consumed 1

    # Now `remaining()` itself consumes one more RPM.
    rpm_after_remaining, _ = await rb.remaining("openai", "gpt-4o")
    assert rpm_after_remaining == 8


async def test_cross_provider_isolation(bucket):
    """Draining `openai/gpt-4o` must not touch `anthropic/haiku`."""
    rb, _ = bucket
    # Drain openai/gpt-4o fully (cap = 10 RPM).
    for _ in range(10):
        ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
        assert ok
    ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
    assert ok is False  # confirm drained

    # anthropic/haiku is fresh — cap = 60 RPM, 60_000 TPM.
    ok, rpm, tpm = await rb.try_acquire("anthropic", "haiku", request_tokens=1)
    assert ok is True
    assert rpm == 59
    assert tpm == 60_000 - 1


async def test_request_tokens_zero_succeeds(bucket):
    """`request_tokens=0` on a fresh bucket: True, full TPM, only 1 RPM gone."""
    rb, _ = bucket
    ok, rpm, tpm = await rb.try_acquire("openai", "gpt-4o", request_tokens=0)
    assert ok is True
    assert rpm == 9  # 1 RPM still consumed by the acquire
    assert tpm == 1000  # TPM untouched


async def test_refill_caps_at_capacity(bucket):
    """Refill is clamped by `math.min(rpm_cap, ...)` in the Lua.

    Drain fully, jump 5 minutes; next acquire should see ~cap-1, not 5*cap.
    """
    rb, clock = bucket
    # Drain RPM (cap=10).
    for _ in range(10):
        ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
        assert ok
    ok, _, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
    assert ok is False  # drained

    # Jump 5 minutes => 5 * 60_000 ms. Refill at 10/60s would naively produce
    # 50 RPM, but the Lua's `math.min(rpm_cap, ...)` clamps it at 10.
    clock[0] = 5 * 60_000
    ok, rpm, _ = await rb.try_acquire("openai", "gpt-4o", request_tokens=1)
    assert ok is True
    # After acquire of 1 RPM: cap (10) - 1 == 9. (Not 50 - 1 == 49.)
    assert rpm == 9
