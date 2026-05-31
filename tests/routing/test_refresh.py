"""Tests for gateway/routing/refresh.py.

TDD step 8. The refresh task aggregates the rolling observation window from
Redis (via observe.aggregate) plus the current bucket remaining (via
ratelimit) plus breaker snapshot, and produces a `CandidateSignals` map for
each candidate in every tier.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.breaker import BreakerSet, BreakerState
from gateway.models import (
    CandidateRef,
    Config,
    RateLimitEntry,
    RoutingConfig,
    TierConfig,
    TierEntry,
)
from gateway.ratelimit import RedisTokenBucket
from gateway.redis_state import RedisState
from gateway.routing.observe import Observer
from gateway.routing.refresh import RefreshTask, build_signals
from gateway.routing.weights import WeightEngine


pytestmark = pytest.mark.asyncio


def _config() -> Config:
    return Config.model_validate(
        {
            "provider_mode": "mock",
            "secrets_mode": "mock",
            "tiers": {
                "fast": {
                    "candidates": [
                        {"provider": "openai", "model": "gpt-4o-mini", "weight": 50.0,
                         "rate_limits": {"rpm": 100, "tpm": 10000}},
                        {"provider": "anthropic", "model": "claude-haiku-4-5", "weight": 30.0,
                         "rate_limits": {"rpm": 60, "tpm": 6000}},
                    ],
                },
            },
            "routing": {
                "refresh_interval_ms": 1000,
                "health_window_s": 60,
                "target_latency_s": 3.0,
                "min_weight_floor": 0.02,
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
    limits = {
        f"{c.provider}/{c.model}": c.rate_limits
        for tc in cfg.tiers.values()
        for c in tc.candidates
    }
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
    # so we expect exactly 98 (one acquire above + one inside `remaining()`).
    # Use a closed range so a regression that drops to e.g. 50 doesn't silently
    # satisfy a one-sided `<= 98` check (t-1 §13.1).
    assert 97 <= s.rpm_remaining <= 99
    # Both reads see 100 TPM consumed; the 0-token read is free of TPM.
    assert s.tpm_remaining <= 9900


async def test_signals_reflect_breaker_open(env):
    cfg, obs, rb, bk, clock_obs, clock_bk = env
    cand = CandidateRef(provider="anthropic", model="claude-haiku-4-5")
    # Trip the breaker
    for _ in range(20):
        await bk.record_failure("anthropic", "claude-haiku-4-5")
    clock_bk[0] = 100.0
    await bk.refresh_snapshot()
    sigs = await build_signals(cfg, obs, rb, bk)
    assert sigs[cand].breaker is BreakerState.OPEN


async def test_signals_per_tier_coverage(env):
    cfg, obs, rb, bk, _, _ = env
    sigs = await build_signals(cfg, obs, rb, bk)
    keys = {(c.provider, c.model) for c in sigs}
    assert ("openai", "gpt-4o-mini") in keys
    assert ("anthropic", "claude-haiku-4-5") in keys


async def test_signals_feed_weight_engine(env):
    cfg, obs, rb, bk, _, _ = env
    sigs = await build_signals(cfg, obs, rb, bk)
    eng = WeightEngine(routing=cfg.routing)
    eng.update_cache(sigs)
    # Without any errors recorded, picking should succeed for the fast tier.
    import random
    picked = eng.pick(cfg.tiers["fast"].candidates, exclude=set(), rng=random.Random(0))
    assert picked is not None


# ---------------------------------------------------------------- §13 additions


async def test_refresh_task_tick_updates_engine_cache(env):
    """One manual `await task.tick()` should populate the engine cache."""
    cfg, obs, rb, bk, _, _ = env
    engine = WeightEngine(routing=cfg.routing)
    task = RefreshTask(
        config=cfg, observer=obs, bucket=rb, breakers=bk, engine=engine
    )
    # Pre-tick: cache is empty.
    assert engine.signals_for(CandidateRef(provider="openai", model="gpt-4o-mini")) is None

    await task.tick()

    # Post-tick: every candidate in every configured tier should be cached.
    for cand in (
        CandidateRef(provider="openai", model="gpt-4o-mini"),
        CandidateRef(provider="anthropic", model="claude-haiku-4-5"),
    ):
        s = engine.signals_for(cand)
        assert s is not None
        assert s.rpm_cap > 0


async def test_refresh_task_start_stop_lifecycle(env):
    """Fast refresh interval; verify multiple ticks fire and stop cleans up."""
    cfg, obs, rb, bk, _, _ = env
    # Override the refresh interval so several ticks happen in <200 ms.
    fast_cfg = cfg.model_copy(
        update={"routing": cfg.routing.model_copy(update={"refresh_interval_ms": 20})}
    )
    engine = WeightEngine(routing=fast_cfg.routing)
    task = RefreshTask(
        config=fast_cfg, observer=obs, bucket=rb, breakers=bk, engine=engine
    )
    task.start()
    await asyncio.sleep(0.15)
    await task.stop()

    # After stop: internal task handle is cleared.
    assert task._task is None
    # At least one tick populated the cache.
    assert engine.signals_for(
        CandidateRef(provider="openai", model="gpt-4o-mini")
    ) is not None


async def test_refresh_task_survives_tick_exception(env, monkeypatch):
    """A one-shot exception in `build_signals` must not crash the loop.

    Replace `build_signals` (via the refresh module) so the first call
    raises and subsequent calls succeed. Verify the task keeps running
    and the engine cache eventually populates.
    """
    cfg, obs, rb, bk, _, _ = env
    fast_cfg = cfg.model_copy(
        update={"routing": cfg.routing.model_copy(update={"refresh_interval_ms": 20})}
    )
    engine = WeightEngine(routing=fast_cfg.routing)

    import gateway.routing.refresh as refresh_mod

    real_build_signals = refresh_mod.build_signals
    call_count = {"n": 0}

    async def flaky_build_signals(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom — simulated transient failure")
        return await real_build_signals(*args, **kwargs)

    monkeypatch.setattr(refresh_mod, "build_signals", flaky_build_signals)

    task = RefreshTask(
        config=fast_cfg, observer=obs, bucket=rb, breakers=bk, engine=engine
    )
    task.start()
    await asyncio.sleep(0.15)
    await task.stop()

    # First tick exploded, but later ticks succeeded => cache populated.
    assert call_count["n"] >= 2
    assert engine.signals_for(
        CandidateRef(provider="openai", model="gpt-4o-mini")
    ) is not None


async def test_build_signals_dedups_same_candidate_in_multiple_tiers(env):
    """Same `(provider, model)` in two tiers => first-seen base_weight wins.

    Per `_all_candidates` semantics in `gateway/routing/refresh.py` — it
    tolerates a config smell rather than raising so the refresh task can't
    blow up at runtime.
    """
    _, obs, rb, bk, _, _ = env
    # Build a config where openai/gpt-4o-mini lives in BOTH fast (weight=50)
    # and smart (weight=99). dict-iteration order in Python 3.7+ is insertion
    # order, so `fast` is visited first and its weight (50) wins.
    cfg = Config.model_validate(
        {
            "provider_mode": "mock",
            "secrets_mode": "mock",
            "tiers": {
                "fast": {
                    "candidates": [
                        {"provider": "openai", "model": "gpt-4o-mini", "weight": 50.0,
                         "rate_limits": {"rpm": 100, "tpm": 10000}},
                    ],
                },
                "smart": {
                    "candidates": [
                        {"provider": "openai", "model": "gpt-4o-mini", "weight": 99.0,
                         "rate_limits": {"rpm": 100, "tpm": 10000}},
                    ],
                },
            },
            "routing": {
                "refresh_interval_ms": 1000,
                "health_window_s": 60,
                "target_latency_s": 3.0,
                "min_weight_floor": 0.02,
            },
            "callers": [{"name": "test", "key_hash": "sha256:abc"}],
        }
    )

    sigs = await build_signals(cfg, obs, rb, bk)
    cand = CandidateRef(provider="openai", model="gpt-4o-mini")
    # Single entry (deduped).
    assert list(sigs.keys()) == [cand]
    # First-seen base_weight (`fast`'s 50.0) wins over `smart`'s 99.0.
    assert sigs[cand].base_weight == 50.0


# ---------------------------------------------------------------- #6.2 backoff


async def test_refresh_failures_increment_metric_counter(env, monkeypatch):
    """Each failed tick increments the `gateway_refresh_errors_total` counter."""
    cfg, obs, rb, bk, _, _ = env
    engine = WeightEngine(routing=cfg.routing)

    import gateway.routing.refresh as refresh_mod
    from gateway.metrics import REFRESH_ERRORS_TOTAL

    async def always_fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(refresh_mod, "build_signals", always_fail)

    task = RefreshTask(
        config=cfg.model_copy(
            update={"routing": cfg.routing.model_copy(update={"refresh_interval_ms": 5})}
        ),
        observer=obs,
        bucket=rb,
        breakers=bk,
        engine=engine,
    )

    start = REFRESH_ERRORS_TOTAL._value.get()
    # Manual tick: must raise; counter must be 1 above start.
    with pytest.raises(RuntimeError):
        await task.tick()
    # The counter is incremented by the *loop's* exception-handling path, not
    # by `tick()` itself, so we drive a few iterations via the loop.
    task.start()
    await asyncio.sleep(0.05)
    await task.stop()
    end = REFRESH_ERRORS_TOTAL._value.get()
    assert end - start >= 2, (
        f"expected at least 2 increments from loop ticks, got {end - start}"
    )


async def test_refresh_backs_off_under_consecutive_failures(env, monkeypatch):
    """Consecutive failures should slow the tick rate down — not stay at the
    base interval. Concretely: after the loop runs for some wall-clock time,
    a constantly-failing tick must produce fewer attempts than `time / base`.
    """
    cfg, obs, rb, bk, _, _ = env
    engine = WeightEngine(routing=cfg.routing)

    import gateway.routing.refresh as refresh_mod

    call_count = {"n": 0}

    async def always_fail(*_args, **_kwargs):
        call_count["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(refresh_mod, "build_signals", always_fail)

    fast_cfg = cfg.model_copy(
        update={"routing": cfg.routing.model_copy(update={"refresh_interval_ms": 10})}
    )
    task = RefreshTask(
        config=fast_cfg, observer=obs, bucket=rb, breakers=bk, engine=engine
    )
    task.start()
    await asyncio.sleep(0.4)
    await task.stop()

    # Base interval 10ms × 0.4s = 40 ticks at no backoff. With exponential
    # backoff doubling on each failure (jittered), we should see well under
    # half that count. Pick an aggressive ceiling so a regression that
    # removes the backoff would fail this test loudly.
    assert call_count["n"] < 20, (
        f"expected backoff to slow ticks below 20 in 400ms, got {call_count['n']}"
    )
    # And at least 2 — we *should* still retry, just less aggressively.
    assert call_count["n"] >= 2


async def test_refresh_resets_backoff_after_success(env, monkeypatch):
    """Once a tick succeeds, the loop should return to the base interval —
    the next consecutive failure must not inherit the previous backoff."""
    cfg, obs, rb, bk, _, _ = env
    engine = WeightEngine(routing=cfg.routing)

    import gateway.routing.refresh as refresh_mod

    real = refresh_mod.build_signals
    call_count = {"n": 0}

    async def flaky(*args, **kwargs):
        call_count["n"] += 1
        # Fail x3, succeed once, then fail again — sequence ends with a
        # failure that should NOT inherit the earlier-backed-off delay.
        if call_count["n"] in (1, 2, 3):
            raise RuntimeError("boom")
        if call_count["n"] == 4:
            return await real(*args, **kwargs)
        raise RuntimeError("boom-again")

    monkeypatch.setattr(refresh_mod, "build_signals", flaky)

    fast_cfg = cfg.model_copy(
        update={"routing": cfg.routing.model_copy(update={"refresh_interval_ms": 20})}
    )
    task = RefreshTask(
        config=fast_cfg, observer=obs, bucket=rb, breakers=bk, engine=engine
    )
    task.start()
    # Long enough to: 3 fails (~20+40+80ms wait windows worst case), 1 success
    # (resets), and a 5th failure (~20ms wait).
    await asyncio.sleep(0.6)
    await task.stop()

    # After reset, a 5th call should have happened within the post-success
    # base-interval window. If reset didn't work, we'd still be in 80+ms
    # backoff and probably not have hit n=5 yet.
    assert call_count["n"] >= 5, (
        f"expected reset-on-success to allow ≥5 ticks in 600ms, got {call_count['n']}"
    )


async def test_refresh_only_logs_first_failure_in_a_burst(env, monkeypatch, caplog):
    """Burst of failures shouldn't spam the log. The first failure should be
    logged at ERROR; subsequent failures in the same burst should not."""
    cfg, obs, rb, bk, _, _ = env
    engine = WeightEngine(routing=cfg.routing)

    import gateway.routing.refresh as refresh_mod
    import logging as stdlib_logging

    async def always_fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(refresh_mod, "build_signals", always_fail)

    fast_cfg = cfg.model_copy(
        update={"routing": cfg.routing.model_copy(update={"refresh_interval_ms": 5})}
    )
    task = RefreshTask(
        config=fast_cfg, observer=obs, bucket=rb, breakers=bk, engine=engine
    )

    with caplog.at_level(stdlib_logging.ERROR, logger="gateway.routing.refresh"):
        task.start()
        await asyncio.sleep(0.2)
        await task.stop()

    # Burst of ~3+ failures should produce at most one ERROR record.
    error_records = [
        r for r in caplog.records
        if r.name == "gateway.routing.refresh" and r.levelno >= stdlib_logging.ERROR
    ]
    assert len(error_records) <= 1, (
        f"expected ≤1 ERROR log in failure burst, got {len(error_records)}"
    )
