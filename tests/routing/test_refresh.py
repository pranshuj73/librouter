"""Tests for gateway/routing/refresh.py.

TDD step 8. The refresh task aggregates the rolling observation window from
Redis (via observe.aggregate) plus the current bucket remaining (via
ratelimit) plus breaker snapshot, and produces a `CandidateSignals` map for
each candidate in every tier.
"""

from __future__ import annotations

import pytest

from gateway.breaker import BreakerSet, BreakerState
from gateway.models import (
    CandidateRef,
    Config,
    PriceEntry,
    RateLimitEntry,
    RoutingConfig,
    TierEntry,
)
from gateway.ratelimit import RedisTokenBucket
from gateway.redis_state import RedisState
from gateway.routing.observe import Observer
from gateway.routing.refresh import build_signals
from gateway.routing.weights import WeightEngine


pytestmark = pytest.mark.asyncio


def _config() -> Config:
    return Config.model_validate(
        {
            "provider_mode": "mock",
            "secrets_mode": "mock",
            "tiers": {
                "fast": [
                    {"provider": "openai", "model": "gpt-4o-mini", "weight": 50.0},
                    {"provider": "anthropic", "model": "haiku", "weight": 30.0},
                ],
            },
            "routing": {
                "refresh_interval_ms": 1000,
                "health_window_s": 60,
                "target_latency_s": 3.0,
                "min_weight_floor": 0.02,
            },
            "prices": {
                "openai/gpt-4o-mini": {"input": 0.15, "output": 0.6},
                "anthropic/haiku": {"input": 1.0, "output": 5.0},
            },
            "rate_limits": {
                "openai/gpt-4o-mini": {"rpm": 100, "tpm": 10000},
                "anthropic/haiku": {"rpm": 60, "tpm": 6000},
            },
            "callers": [{"name": "test", "key_hash": "sha256:abc"}],
        }
    )


@pytest.fixture
async def env(redis):
    state = RedisState(redis)
    await state.load_scripts()
    clock_obs = [100.0]
    clock_bk = [100.0]
    cfg = _config()
    obs = Observer(state=state, window_s=60, now_s_fn=lambda: clock_obs[0])
    bk = BreakerSet(state=state, now_s_fn=lambda: clock_bk[0])
    limits = cfg.rate_limits
    rb = RedisTokenBucket(state=state, limits=limits, now_ms_fn=lambda: int(clock_obs[0] * 1000))
    return cfg, obs, rb, bk, clock_obs, clock_bk


async def test_signals_for_healthy_candidate(env):
    cfg, obs, rb, bk, clock_obs, _ = env
    cand = CandidateRef(provider="openai", model="gpt-4o-mini")
    # Record some successes
    for i in range(5):
        clock_obs[0] = 100.0 + i
        await obs.record_success(cand, latency_s=0.5)
    # Consume some bucket at the same instant as the refresh — no refill between.
    await rb.try_acquire("openai", "gpt-4o-mini", request_tokens=100)
    sigs = await build_signals(cfg, obs, rb, bk)
    s = sigs[cand]
    assert s.error_rate == 0.0
    assert s.mean_latency_s == pytest.approx(0.5, rel=0.05)
    assert s.base_weight == 50.0
    assert s.breaker is BreakerState.CLOSED
    assert s.rpm_cap == 100
    assert s.tpm_cap == 10000
    # The remaining-read consumes one more RPM (acquire-with-0-tokens trick),
    # so we expect <= 98 (one acquire above + one inside `remaining()`).
    assert s.rpm_remaining <= 98
    # Both reads see 100 TPM consumed; the 0-token read is free of TPM.
    assert s.tpm_remaining <= 9900


async def test_signals_reflect_breaker_open(env):
    cfg, obs, rb, bk, clock_obs, clock_bk = env
    cand = CandidateRef(provider="anthropic", model="haiku")
    # Trip the breaker
    for _ in range(20):
        await bk.record_failure("anthropic", "haiku")
    clock_bk[0] = 100.0
    await bk.refresh_snapshot()
    sigs = await build_signals(cfg, obs, rb, bk)
    assert sigs[cand].breaker is BreakerState.OPEN


async def test_signals_per_tier_coverage(env):
    cfg, obs, rb, bk, _, _ = env
    sigs = await build_signals(cfg, obs, rb, bk)
    keys = {(c.provider, c.model) for c in sigs}
    assert ("openai", "gpt-4o-mini") in keys
    assert ("anthropic", "haiku") in keys


async def test_signals_feed_weight_engine(env):
    cfg, obs, rb, bk, _, _ = env
    sigs = await build_signals(cfg, obs, rb, bk)
    eng = WeightEngine(routing=cfg.routing)
    eng.update_cache(sigs)
    # Without any errors recorded, picking should succeed for the fast tier.
    import random
    picked = eng.pick(cfg.tiers["fast"], exclude=set(), rng=random.Random(0))
    assert picked is not None
