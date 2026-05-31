"""Tests for gateway/ratelimit.py.

TDD step 4. RedisTokenBucket wraps redis_state.ratelimit_acquire with a clean
API: per (provider, model) capacity from config, deterministic clock via an
injected `clock_ms_fn`, plus `clamp` for vendor-header-driven shrinkage.
"""

from __future__ import annotations

import pytest

from gateway.models import RateLimitEntry
from gateway.ratelimit import RedisTokenBucket
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


async def test_unknown_provider_model_raises(bucket):
    rb, _ = bucket
    with pytest.raises(KeyError):
        await rb.try_acquire("nope", "missing", request_tokens=1)
