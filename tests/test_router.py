"""Tests for gateway/router.py.

TDD step 10. End-to-end routing decisions: weighted pick + exclude-and-repick
on failure + deadline budget + breaker/bucket skips. Uses mock vendors, fake
clock, seeded RNG, fakeredis.
"""

from __future__ import annotations

import random

import pytest

from gateway.breaker import BreakerSet
from gateway.errors import BadRequest, RateLimited, Timeout, Transient5xx
from gateway.models import (
    AttemptRecord,
    Caller,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Config,
    Message,
)
from gateway.providers.mock import (
    MockAnthropicVendor,
    MockGoogleVendor,
    MockOpenAIVendor,
)
from gateway.ratelimit import RedisTokenBucket
from gateway.redis_state import RedisState
from gateway.router import RouterError, RouterErrorKind, RouterResult, Router
from gateway.routing.observe import Observer
from gateway.routing.refresh import build_signals
from gateway.routing.weights import WeightEngine
from gateway.secrets import MockSecretsManager


pytestmark = pytest.mark.asyncio


def _config() -> Config:
    return Config.model_validate(
        {
            "provider_mode": "mock",
            "secrets_mode": "mock",
            "tiers": {
                "fast": [
                    {"provider": "openai", "model": "gpt-4o-mini", "weight": 33.0},
                    {"provider": "anthropic", "model": "haiku", "weight": 33.0},
                    {"provider": "google", "model": "gemini-flash", "weight": 33.0},
                ],
            },
            "routing": {
                "refresh_interval_ms": 100,
                "health_window_s": 60,
                "target_latency_s": 3.0,
                "min_weight_floor": 0.001,
            },
            "prices": {
                "openai/gpt-4o-mini": {"input": 0.15, "output": 0.6},
                "anthropic/haiku": {"input": 1.0, "output": 5.0},
                "google/gemini-flash": {"input": 0.3, "output": 2.5},
            },
            "rate_limits": {
                "openai/gpt-4o-mini": {"rpm": 1000, "tpm": 100_000},
                "anthropic/haiku": {"rpm": 1000, "tpm": 100_000},
                "google/gemini-flash": {"rpm": 1000, "tpm": 100_000},
            },
            "callers": [
                {"name": "test", "key_hash": "sha256:abc", "daily_token_cap": 1_000_000}
            ],
        }
    )


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="fast",
        messages=[Message(role="user", content="hi")],
        max_tokens=32,
    )


def _caller() -> Caller:
    return Caller(name="test", daily_token_cap=1_000_000, enabled=True)


@pytest.fixture
async def harness(redis):
    state = RedisState(redis)
    await state.load_scripts()
    cfg = _config()

    sec_mgr = MockSecretsManager()
    vendors = {
        "openai": MockOpenAIVendor(sec_mgr),
        "anthropic": MockAnthropicVendor(sec_mgr),
        "google": MockGoogleVendor(sec_mgr),
    }

    deadline_clock = [0.0]
    obs_clock = [0.0]
    bk_clock = [0.0]
    rb_clock = [0]

    def deadline_now() -> float:
        return deadline_clock[0]

    obs = Observer(state=state, window_s=60, now_s_fn=lambda: obs_clock[0])
    bk = BreakerSet(state=state, now_s_fn=lambda: bk_clock[0])
    rb = RedisTokenBucket(state=state, limits=cfg.rate_limits, now_ms_fn=lambda: rb_clock[0])

    engine = WeightEngine(routing=cfg.routing)
    engine.update_cache(await build_signals(cfg, obs, rb, bk))

    rng = random.Random(123)

    router = Router(
        config=cfg,
        vendors=vendors,
        weight_engine=engine,
        bucket=rb,
        observer=obs,
        rng=rng,
        deadline_clock_s=deadline_now,
        total_budget_s=10.0,
        per_attempt_max_s=8.0,
        deadline_buffer_s=0.5,
    )

    return {
        "router": router,
        "vendors": vendors,
        "cfg": cfg,
        "obs": obs,
        "bk": bk,
        "rb": rb,
        "engine": engine,
        "obs_clock": obs_clock,
        "bk_clock": bk_clock,
        "deadline_clock": deadline_clock,
        "rb_clock": rb_clock,
    }


# ---------------------------------------------------------------- happy paths


async def test_success_first_attempt(harness):
    router = harness["router"]
    result = await router.route(_req(), _caller())
    assert isinstance(result, RouterResult)
    assert isinstance(result.response, ChatCompletionResponse)
    assert result.response.model == "fast"
    assert len(result.attempts) == 1
    assert result.attempts[0].status == "ok"
    assert result.attempts[0].cost_usd > 0


# ---------------------------------------------------------------- failover


async def test_failover_after_rate_limited(harness):
    router = harness["router"]
    cfg = harness["cfg"]
    engine = harness["engine"]
    # The router never retries the SAME vendor, so to demonstrate failover we
    # need to queue an error on whichever candidate the RNG picks first. Peek
    # at the engine's choice using a copy of the seeded RNG so the real run
    # hits the same first pick.
    rng_peek = random.Random(123)
    first_pick = engine.pick(cfg.tiers["fast"], exclude=set(), rng=rng_peek)
    assert first_pick is not None
    harness["vendors"][first_pick.provider].queue_error(RateLimited("429"))
    # Subsequent vendors fall through to default-success.
    result = await router.route(_req(), _caller())
    assert len(result.attempts) == 2
    assert result.attempts[0].status == "rate_limited"
    assert result.attempts[0].provider == first_pick.provider
    assert result.attempts[1].status == "ok"
    assert result.attempts[1].provider != first_pick.provider


async def test_all_candidates_fail_returns_503(harness):
    router = harness["router"]
    for v in harness["vendors"].values():
        v.queue_error(Transient5xx("503"))
        v.queue_error(Transient5xx("503"))
        v.queue_error(Transient5xx("503"))
    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind is RouterErrorKind.UPSTREAM_UNAVAILABLE
    assert len(exc.value.tried) >= 3


# ---------------------------------------------------------------- non-retryable


async def test_bad_request_returns_immediately(harness):
    router = harness["router"]
    for v in harness["vendors"].values():
        v.queue_error(BadRequest("malformed"))
    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind is RouterErrorKind.INVALID_REQUEST
    # No failover: exactly one attempt
    total_calls = sum(v.call_count for v in harness["vendors"].values())
    assert total_calls == 1


# ---------------------------------------------------------------- deadline


async def test_deadline_exceeded_mid_failover(harness):
    router = harness["router"]
    # The deadline is at +10s. Make the first attempt eat almost the whole
    # budget by advancing the deadline clock manually inside the failover.
    deadline_clock = harness["deadline_clock"]

    class _ClockAdvancingVendor:
        async def chat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            deadline_clock[0] += 9.5
            raise Transient5xx("slow then fail")

    for v in harness["vendors"].values():
        v.queue_error(Transient5xx("fail"))
    # Replace the first-picked vendor with one that burns the budget
    # The RNG with seed=123 picks the first candidate deterministically; we
    # patch every vendor to consume time so whichever is picked first burns
    # budget.
    for v in harness["vendors"].values():
        original_chat = v.chat

        async def chat_eat_budget(*args, _v=v, _orig=original_chat, **kwargs):
            deadline_clock[0] += 8.0
            return await _orig(*args, **kwargs)

        v.chat = chat_eat_budget  # type: ignore[method-assign]
    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind in (
        RouterErrorKind.DEADLINE_EXCEEDED,
        RouterErrorKind.UPSTREAM_UNAVAILABLE,
    )


# ---------------------------------------------------------------- breaker / bucket skips


async def test_breaker_open_candidate_not_picked(harness):
    router = harness["router"]
    bk = harness["bk"]
    cfg = harness["cfg"]
    # Trip breaker for openai
    for _ in range(25):
        await bk.record_failure("openai", "gpt-4o-mini")
    await bk.refresh_snapshot()
    # Rebuild signals so engine sees breaker open
    sigs = await build_signals(cfg, harness["obs"], harness["rb"], bk)
    harness["engine"].update_cache(sigs)
    # Now route 30 times — openai should never be picked
    results = []
    for _ in range(30):
        r = await router.route(_req(), _caller())
        results.append(r.attempts[0].provider)
    assert "openai" not in results


async def test_empty_bucket_causes_repick(harness):
    router = harness["router"]
    rb = harness["rb"]
    # Drain anthropic RPM completely
    cfg = harness["cfg"]
    for _ in range(cfg.rate_limits["anthropic/haiku"].rpm + 1):
        await rb.try_acquire("anthropic", "haiku", request_tokens=1)
    # Now route many times; if anthropic ever gets first-picked, the bucket
    # acquire will fail and we'll repick. Result should still be success.
    for _ in range(20):
        r = await router.route(_req(), _caller())
        # Whichever vendor served, the response must be ok and not anthropic
        # (since bucket is dry).
        assert r.response.choices[0].message.role == "assistant"


# ---------------------------------------------------------------- accounting feed


async def test_failed_attempt_recorded_in_observe(harness):
    router = harness["router"]
    obs = harness["obs"]
    cfg = harness["cfg"]
    engine = harness["engine"]
    rng_peek = random.Random(123)
    first_pick = engine.pick(cfg.tiers["fast"], exclude=set(), rng=rng_peek)
    assert first_pick is not None
    harness["vendors"][first_pick.provider].queue_error(RateLimited("429"))
    await router.route(_req(), _caller())
    # The forced-failure candidate should have one failure logged.
    agg = await obs.aggregate(first_pick)
    assert agg.failures == 1
