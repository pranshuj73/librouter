"""Tests for gateway/router.py.

TDD step 10. End-to-end routing decisions: weighted pick + exclude-and-repick
on failure + deadline budget + breaker/bucket skips. Uses mock vendors, fake
clock, seeded RNG, fakeredis.
"""

from __future__ import annotations

import re
import random
from dataclasses import dataclass

import pytest

from gateway.breaker import BreakerSet
from gateway.errors import (
    AuthError,
    BadRequest,
    ContentFiltered,
    RateLimited,
    Timeout,
    Transient5xx,
)
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
                    {"provider": "anthropic", "model": "claude-haiku-4-5", "weight": 33.0},
                    {"provider": "google", "model": "gemini-2.5-flash", "weight": 33.0},
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
                "anthropic/claude-haiku-4-5": {"input": 1.0, "output": 5.0},
                "google/gemini-2.5-flash": {"input": 0.3, "output": 2.5},
            },
            "rate_limits": {
                "openai/gpt-4o-mini": {"rpm": 1000, "tpm": 100_000},
                "anthropic/claude-haiku-4-5": {"rpm": 1000, "tpm": 100_000},
                "google/gemini-2.5-flash": {"rpm": 1000, "tpm": 100_000},
            },
            "callers": [
                {"name": "test", "key_hash": "sha256:abc", "daily_token_cap": 1_000_000}
            ],
        }
    )


def _req(metadata: dict | None = None) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="fast",
        messages=[Message(role="user", content="hi")],
        max_tokens=32,
        metadata=metadata,
    )


def _caller() -> Caller:
    return Caller(name="test", daily_token_cap=1_000_000, enabled=True)


@dataclass
class Harness:
    router: Router
    vendors: dict
    cfg: Config
    obs: Observer
    bk: BreakerSet
    rb: RedisTokenBucket
    engine: WeightEngine
    obs_clock: list
    bk_clock: list
    deadline_clock: list
    rb_clock: list


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

    return Harness(
        router=router,
        vendors=vendors,
        cfg=cfg,
        obs=obs,
        bk=bk,
        rb=rb,
        engine=engine,
        obs_clock=obs_clock,
        bk_clock=bk_clock,
        deadline_clock=deadline_clock,
        rb_clock=rb_clock,
    )


# ---------------------------------------------------------------- happy paths


async def test_success_first_attempt(harness):
    router = harness.router
    result = await router.route(_req(), _caller())
    assert isinstance(result, RouterResult)
    assert isinstance(result.response, ChatCompletionResponse)
    assert result.response.model == "fast"
    assert len(result.attempts) == 1
    assert result.attempts[0].status == "ok"
    assert result.attempts[0].cost_usd > 0


# ---------------------------------------------------------------- failover


async def test_failover_after_rate_limited(harness):
    """First attempt rate-limited, second succeeds on a different vendor.

    Rather than peeking at the seeded RNG to figure out which vendor will
    be picked first (which couples the test to ``engine.pick``'s internal
    RNG-consumption pattern — see t-1 §9.3), we install a one-shot wrapper
    that raises ``RateLimited`` on the *very first* vendor call across all
    vendors, and lets subsequent calls fall through to scripted success.
    Whichever vendor the RNG picks first will fail; the router excludes
    it and picks a different one, which succeeds.
    """
    router = harness.router
    first_call_done = [False]
    for v in harness.vendors.values():
        v.queue_success()
        original_chat = v.chat

        async def chat_one_shot_fail(
            model, messages, params, timeout_s, _orig=original_chat
        ):  # type: ignore[no-untyped-def]
            if not first_call_done[0]:
                first_call_done[0] = True
                raise RateLimited("429")
            return await _orig(model, messages, params, timeout_s)

        v.chat = chat_one_shot_fail  # type: ignore[method-assign]

    result = await router.route(_req(), _caller())
    assert len(result.attempts) == 2
    assert result.attempts[0].status == "rate_limited"
    assert result.attempts[1].status == "ok"
    assert result.attempts[0].provider != result.attempts[1].provider


async def test_all_candidates_fail_returns_503(harness):
    router = harness.router
    for v in harness.vendors.values():
        v.queue_error(Transient5xx("503"))
        v.queue_error(Transient5xx("503"))
        v.queue_error(Transient5xx("503"))
    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind is RouterErrorKind.UPSTREAM_UNAVAILABLE
    assert len(exc.value.tried) >= 3


# ---------------------------------------------------------------- non-retryable


async def test_bad_request_returns_immediately(harness):
    router = harness.router
    for v in harness.vendors.values():
        v.queue_error(BadRequest("malformed"))
    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind is RouterErrorKind.INVALID_REQUEST
    # No failover: exactly one attempt
    total_calls = sum(v.call_count for v in harness.vendors.values())
    assert total_calls == 1


async def test_auth_error_propagates_as_401(harness):
    """AuthError is non-retryable; router maps it to AUTH (401)."""
    router = harness.router
    for v in harness.vendors.values():
        v.queue_error(AuthError("bad key"))
    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind is RouterErrorKind.AUTH
    assert exc.value.body.type == "auth"
    total_calls = sum(v.call_count for v in harness.vendors.values())
    assert total_calls == 1


async def test_content_filtered_propagates_as_400(harness):
    """ContentFiltered is non-retryable; router maps it to INVALID_REQUEST (400)
    with body type 'content_filtered'."""
    router = harness.router
    for v in harness.vendors.values():
        v.queue_error(ContentFiltered("refused"))
    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind is RouterErrorKind.INVALID_REQUEST
    assert exc.value.body.type == "content_filtered"
    total_calls = sum(v.call_count for v in harness.vendors.values())
    assert total_calls == 1


# ---------------------------------------------------------------- deadline


async def test_deadline_exceeded_mid_failover(harness):
    """A first attempt eats ~9.5s of the 10s budget then raises Transient5xx;
    on the next iteration ``remaining < 1.5`` and the router returns
    DEADLINE_EXCEEDED strictly (not UPSTREAM_UNAVAILABLE).

    To make the behavior deterministic we monkey-patch every vendor's
    ``chat`` with a one-shot wrapper that advances the clock and raises.
    On the second iteration the router exits with DEADLINE_EXCEEDED before
    any vendor is called again.
    """
    router = harness.router
    deadline_clock = harness.deadline_clock
    advanced = [False]

    for v in harness.vendors.values():
        async def chat_eat_then_fail(
            *args, _v=v, **kwargs
        ):  # type: ignore[no-untyped-def]
            if not advanced[0]:
                deadline_clock[0] += 9.5
                advanced[0] = True
            raise Transient5xx("eat-budget")

        v.chat = chat_eat_then_fail  # type: ignore[method-assign]

    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    assert exc.value.kind is RouterErrorKind.DEADLINE_EXCEEDED
    assert len(exc.value.tried) >= 1


async def test_per_attempt_timeout_shrinks_near_deadline(redis):
    """With ``remaining=1.5s`` and ``deadline_buffer_s=0.5s``, the per-attempt
    timeout passed to the vendor should be ``min(max(0.1, 1.0), 8.0) == 1.0``.

    Driven by a clock that yields 0.0 the first time (when the deadline is
    computed) and 0.5 thereafter (so ``remaining = 2.0 - 0.5 = 1.5``).
    """
    state = RedisState(redis)
    await state.load_scripts()
    cfg = _config()
    sec_mgr = MockSecretsManager()
    vendors = {
        "openai": MockOpenAIVendor(sec_mgr),
        "anthropic": MockAnthropicVendor(sec_mgr),
        "google": MockGoogleVendor(sec_mgr),
    }
    for v in vendors.values():
        v.queue_success()

    obs = Observer(state=state, window_s=60, now_s_fn=lambda: 0.0)
    bk = BreakerSet(state=state, now_s_fn=lambda: 0.0)
    rb = RedisTokenBucket(state=state, limits=cfg.rate_limits, now_ms_fn=lambda: 0)
    engine = WeightEngine(routing=cfg.routing)
    engine.update_cache(await build_signals(cfg, obs, rb, bk))

    call_counter = [0]

    def clock() -> float:
        # 1st call: deadline = 0 + 2 = 2; subsequent calls return 0.5 so
        # remaining = 2 - 0.5 = 1.5.
        call_counter[0] += 1
        return 0.0 if call_counter[0] == 1 else 0.5

    router = Router(
        config=cfg,
        vendors=vendors,
        weight_engine=engine,
        bucket=rb,
        observer=obs,
        rng=random.Random(123),
        deadline_clock_s=clock,
        total_budget_s=2.0,
        per_attempt_max_s=8.0,
        deadline_buffer_s=0.5,
    )

    seen_timeouts: list[float] = []
    for v in vendors.values():
        original_chat = v.chat

        async def chat_capture(
            model, messages, params, timeout_s, _orig=original_chat
        ):  # type: ignore[no-untyped-def]
            seen_timeouts.append(timeout_s)
            return await _orig(model, messages, params, timeout_s)

        v.chat = chat_capture  # type: ignore[method-assign]

    result = await router.route(_req(), _caller())
    assert result.attempts[0].status == "ok"
    assert len(seen_timeouts) == 1
    assert seen_timeouts[0] == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------- breaker / bucket skips


async def test_breaker_open_candidate_not_picked(harness):
    router = harness.router
    bk = harness.bk
    cfg = harness.cfg
    # Trip breaker for openai
    for _ in range(25):
        await bk.record_failure("openai", "gpt-4o-mini")
    await bk.refresh_snapshot()
    # Rebuild signals so engine sees breaker open
    sigs = await build_signals(cfg, harness.obs, harness.rb, bk)
    harness.engine.update_cache(sigs)
    # Now route 30 times — openai should never be picked
    results = []
    for _ in range(30):
        r = await router.route(_req(), _caller())
        results.append(r.attempts[0].provider)
    assert "openai" not in results


async def test_empty_bucket_causes_repick(harness):
    router = harness.router
    rb = harness.rb
    # Drain anthropic RPM completely
    cfg = harness.cfg
    for _ in range(cfg.rate_limits["anthropic/claude-haiku-4-5"].rpm + 1):
        await rb.try_acquire("anthropic", "claude-haiku-4-5", request_tokens=1)
    # Now route many times; if anthropic ever gets first-picked, the bucket
    # acquire will fail and the router repicks. Result must still be ok AND
    # never resolve to anthropic.
    seen_providers: set[str] = set()
    for _ in range(20):
        r = await router.route(_req(), _caller())
        assert r.response.choices[0].message.role == "assistant"
        for att in r.attempts:
            assert att.provider != "anthropic"
            seen_providers.add(att.provider)
    assert "anthropic" not in seen_providers


async def test_vendor_missing_skips_candidate(redis):
    """If the router's vendors dict is missing a provider listed in the tier,
    the router records ``vendor_missing`` in ``tried`` and excludes that
    candidate. Across many routes the missing provider should never be the
    resolved attempt provider.
    """
    state = RedisState(redis)
    await state.load_scripts()
    cfg = _config()
    sec_mgr = MockSecretsManager()
    # Deliberately omit 'google' from the vendors dict — tier still references it.
    vendors = {
        "openai": MockOpenAIVendor(sec_mgr),
        "anthropic": MockAnthropicVendor(sec_mgr),
    }
    obs = Observer(state=state, window_s=60, now_s_fn=lambda: 0.0)
    bk = BreakerSet(state=state, now_s_fn=lambda: 0.0)
    rb = RedisTokenBucket(state=state, limits=cfg.rate_limits, now_ms_fn=lambda: 0)
    engine = WeightEngine(routing=cfg.routing)
    engine.update_cache(await build_signals(cfg, obs, rb, bk))
    router = Router(
        config=cfg,
        vendors=vendors,
        weight_engine=engine,
        bucket=rb,
        observer=obs,
        rng=random.Random(123),
        deadline_clock_s=lambda: 0.0,
    )

    saw_vendor_missing = False
    for _ in range(40):
        # Capture tried via instrumenting: run the route and read attempts.
        # The successful attempt is always non-google. If google was picked
        # first, it would have been logged as vendor_missing — observed by
        # the fact that a successful non-google attempt has attempt_idx==0
        # (router didn't record vendor_missing as an AttemptRecord; only in
        # tried). So we inspect by routing repeatedly and ensuring google
        # never resolves.
        result = await router.route(_req(), _caller())
        assert result.attempts[-1].provider != "google"

    # Now force google to be picked first by excluding others via empty
    # buckets, then assert vendor_missing surfaces in `tried` on the
    # resulting RouterError. Drain openai and anthropic.
    for _ in range(cfg.rate_limits["openai/gpt-4o-mini"].rpm + 1):
        await rb.try_acquire("openai", "gpt-4o-mini", request_tokens=1)
    for _ in range(cfg.rate_limits["anthropic/claude-haiku-4-5"].rpm + 1):
        await rb.try_acquire("anthropic", "claude-haiku-4-5", request_tokens=1)

    with pytest.raises(RouterError) as exc:
        await router.route(_req(), _caller())
    # When all candidates are excluded (bucket_empty for openai+anthropic,
    # vendor_missing for google), router exits with UPSTREAM_UNAVAILABLE.
    assert exc.value.kind is RouterErrorKind.UPSTREAM_UNAVAILABLE
    statuses = [reason for _cand, reason in exc.value.tried]
    saw_vendor_missing = "vendor_missing" in statuses
    assert saw_vendor_missing, f"expected 'vendor_missing' in tried, got {statuses}"


# ---------------------------------------------------------------- pricing / id-flow


async def test_cost_usd_matches_pricing_table(harness):
    """cost_usd on the successful attempt equals the PricingTable value.

    With seed 123 the RNG picks openai/gpt-4o-mini first. We verify the
    router cost matches what load_pricing() computes directly.
    """
    import math
    from gateway.pricing import load_pricing

    router = harness.router
    result = await router.route(_req(), _caller())
    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.status == "ok"
    assert attempt.provider == "openai"
    assert attempt.model == "gpt-4o-mini"

    table = load_pricing()
    expected_cost = table.cost_usd(
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=attempt.input_tokens,
        output_tokens=attempt.output_tokens,
    )
    assert expected_cost > 0.0
    assert math.isclose(attempt.cost_usd, expected_cost, rel_tol=1e-9), (
        f"expected {expected_cost}, got {attempt.cost_usd}"
    )


async def test_cost_is_zero_when_price_missing(redis):
    """If a candidate's model is not in the PricingTable, cost_usd is 0.0.

    We build a router whose tier uses a made-up model name that is guaranteed
    not to appear in the vendored JSON. No Config mutation or PricingTable
    subclassing needed — the model name simply won't match any JSON entry.
    """
    from gateway.models import Config
    from gateway.routing.refresh import build_signals

    state = RedisState(redis)
    await state.load_scripts()

    # Use a model name that does not exist in the pricing JSON.
    unknown_model = "no-such-model-xyzzy-9999"
    cfg = Config.model_validate(
        {
            "provider_mode": "mock",
            "secrets_mode": "mock",
            "tiers": {
                "fast": [
                    {"provider": "google", "model": unknown_model, "weight": 100.0},
                ],
            },
            "routing": {"refresh_interval_ms": 100, "health_window_s": 60,
                        "target_latency_s": 3.0, "min_weight_floor": 0.001},
            "prices": {
                f"google/{unknown_model}": {"input": 0.3, "output": 2.5},
            },
            "rate_limits": {
                f"google/{unknown_model}": {"rpm": 1000, "tpm": 100_000},
            },
            "callers": [{"name": "test", "key_hash": "sha256:abc",
                         "daily_token_cap": 1_000_000}],
        }
    )

    sec_mgr = MockSecretsManager()
    vendors = {"google": MockGoogleVendor(sec_mgr)}
    obs = Observer(state=state, window_s=60, now_s_fn=lambda: 0.0)
    bk = BreakerSet(state=state, now_s_fn=lambda: 0.0)
    rb = RedisTokenBucket(state=state, limits=cfg.rate_limits, now_ms_fn=lambda: 0)
    engine = WeightEngine(routing=cfg.routing)
    engine.update_cache(await build_signals(cfg, obs, rb, bk))

    router = Router(
        config=cfg,
        vendors=vendors,
        weight_engine=engine,
        bucket=rb,
        observer=obs,
        rng=random.Random(1),
        deadline_clock_s=lambda: 0.0,
    )
    result = await router.route(_req(), _caller())
    winner = result.attempts[-1]
    assert winner.status == "ok"
    assert winner.provider == "google"
    assert winner.model == unknown_model
    assert winner.cost_usd == 0.0


async def test_vendor_req_id_flows_to_response_id(harness):
    """response.id is now always the server-generated uuid4 hex, regardless
    of the vendor's vendor_request_id.  The server-generated id is 32 hex
    chars; it takes precedence over any vendor-supplied id."""
    router = harness.router
    for v in harness.vendors.values():
        v.queue_success(vendor_request_id="vrid-explicit")
    result = await router.route(_req(), _caller())
    assert re.fullmatch(r"[0-9a-f]{32}", result.response.id)


# ---------------------------------------------------------------- Finding 3.5 — server-generated request_id


async def test_request_id_is_server_generated(harness):
    """route() with no metadata must return a 32-hex-char server-generated id."""
    router = harness.router
    result = await router.route(_req(), _caller())
    assert re.fullmatch(r"[0-9a-f]{32}", result.response.id), (
        f"expected 32-char hex id, got {result.response.id!r}"
    )


async def test_client_trace_id_is_recorded(harness):
    """Caller-supplied metadata.request_id is stored as client_trace_id;
    response.id is the server-generated uuid (not the caller value)."""
    router = harness.router
    for v in harness.vendors.values():
        v.queue_success()
    result = await router.route(
        _req(metadata={"request_id": "client-abc-123"}), _caller()
    )
    assert result.attempts[0].client_trace_id == "client-abc-123"
    # response.id must be the server-generated uuid, not the caller's value.
    assert result.response.id != "client-abc-123"
    assert re.fullmatch(r"[0-9a-f]{32}", result.response.id)


async def test_client_trace_id_truncated_at_128(harness):
    """A metadata.request_id longer than 128 chars is truncated before storage."""
    router = harness.router
    for v in harness.vendors.values():
        v.queue_success()
    long_id = "x" * 200
    result = await router.route(
        _req(metadata={"request_id": long_id}), _caller()
    )
    assert result.attempts[0].client_trace_id == "x" * 128


# ---------------------------------------------------------------- accounting feed


async def test_failed_attempt_recorded_in_observe(harness):
    """First attempt fails on whichever vendor RNG picks; success on retry.

    See test_failover_after_rate_limited for the rationale on removing the
    RNG-peek pattern (t-1 §9.3 / §9.9). After the route, we determine the
    failed candidate from ``result.attempts[0].provider`` and assert its
    failure was logged; we additionally assert the successful candidate's
    success was logged.
    """
    router = harness.router
    obs = harness.obs
    cfg = harness.cfg
    first_call_done = [False]
    for v in harness.vendors.values():
        v.queue_success()
        original_chat = v.chat

        async def chat_one_shot_fail(
            model, messages, params, timeout_s, _orig=original_chat
        ):  # type: ignore[no-untyped-def]
            if not first_call_done[0]:
                first_call_done[0] = True
                raise RateLimited("429")
            return await _orig(model, messages, params, timeout_s)

        v.chat = chat_one_shot_fail  # type: ignore[method-assign]

    result = await router.route(_req(), _caller())
    assert len(result.attempts) == 2
    failed_provider = result.attempts[0].provider
    winner_provider = result.attempts[1].provider

    failed_cand = next(
        c for c in cfg.tiers["fast"] if c.provider == failed_provider
    )
    winner_cand = next(
        c for c in cfg.tiers["fast"] if c.provider == winner_provider
    )

    from gateway.models import CandidateRef

    agg_failed = await obs.aggregate(
        CandidateRef(provider=failed_cand.provider, model=failed_cand.model)
    )
    agg_winner = await obs.aggregate(
        CandidateRef(provider=winner_cand.provider, model=winner_cand.model)
    )
    assert agg_failed.failures == 1
    assert agg_winner.successes == 1
