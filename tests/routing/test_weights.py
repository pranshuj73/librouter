"""Tests for gateway/routing/weights.py.

TDD step 7. Pure math + seeded RNG — no Redis required. Verifies:
- health_score reflects error rate and latency
- budget_score collapses on bucket exhaustion
- effective_weight = base * health * budget (and zero on breaker_open or floor)
- pick() distribution converges to weight ratio over many seeded trials
- pick() excludes the given set
- pick() returns None when all weights zero
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from gateway.breaker import BreakerState
from gateway.models import CandidateRef, RoutingConfig, TierEntry
from gateway.routing.weights import (
    CandidateSignals,
    WeightEngine,
    budget_score,
    effective_weight,
    health_score,
)


def test_health_score_healthy():
    s = health_score(error_rate=0.0, mean_latency_s=1.0, target_latency_s=3.0)
    # (1 - 0) * (3 / (3 + 1)) = 0.75
    assert s == pytest.approx(0.75)


def test_health_score_zero_when_all_errors():
    assert health_score(error_rate=1.0, mean_latency_s=0.5, target_latency_s=3.0) == 0.0


def test_health_score_degrades_with_latency():
    fast = health_score(error_rate=0.0, mean_latency_s=0.1, target_latency_s=3.0)
    slow = health_score(error_rate=0.0, mean_latency_s=10.0, target_latency_s=3.0)
    assert fast > slow


def test_budget_score_full():
    assert budget_score(rpm_remaining=100, rpm_cap=100, tpm_remaining=1000, tpm_cap=1000) == 1.0


def test_budget_score_min_of_dims():
    assert budget_score(rpm_remaining=10, rpm_cap=100, tpm_remaining=500, tpm_cap=1000) == 0.1


def test_budget_score_zero_when_either_empty():
    assert budget_score(rpm_remaining=0, rpm_cap=100, tpm_remaining=1000, tpm_cap=1000) == 0.0
    assert budget_score(rpm_remaining=100, rpm_cap=100, tpm_remaining=0, tpm_cap=1000) == 0.0


def test_effective_weight_normal_case():
    w = effective_weight(
        base=50.0,
        health=0.75,
        budget=1.0,
        breaker=BreakerState.CLOSED,
        floor=0.02,
    )
    assert w == pytest.approx(37.5)


def test_effective_weight_zero_on_breaker_open():
    w = effective_weight(
        base=50.0, health=1.0, budget=1.0, breaker=BreakerState.OPEN, floor=0.02
    )
    assert w == 0.0


def test_effective_weight_zero_when_below_floor():
    w = effective_weight(
        base=0.5, health=0.01, budget=1.0, breaker=BreakerState.CLOSED, floor=0.02
    )
    assert w == 0.0


def _three_candidate_tier() -> list[TierEntry]:
    return [
        TierEntry(provider="a", model="x", weight=50.0),
        TierEntry(provider="b", model="y", weight=30.0),
        TierEntry(provider="c", model="z", weight=20.0),
    ]


def _all_healthy_signals() -> dict[CandidateRef, CandidateSignals]:
    return {
        CandidateRef(provider="a", model="x"): CandidateSignals(
            base_weight=50.0,
            error_rate=0.0,
            mean_latency_s=0.5,
            rpm_remaining=100,
            rpm_cap=100,
            tpm_remaining=1000,
            tpm_cap=1000,
            breaker=BreakerState.CLOSED,
        ),
        CandidateRef(provider="b", model="y"): CandidateSignals(
            base_weight=30.0,
            error_rate=0.0,
            mean_latency_s=0.5,
            rpm_remaining=100,
            rpm_cap=100,
            tpm_remaining=1000,
            tpm_cap=1000,
            breaker=BreakerState.CLOSED,
        ),
        CandidateRef(provider="c", model="z"): CandidateSignals(
            base_weight=20.0,
            error_rate=0.0,
            mean_latency_s=0.5,
            rpm_remaining=100,
            rpm_cap=100,
            tpm_remaining=1000,
            tpm_cap=1000,
            breaker=BreakerState.CLOSED,
        ),
    }


def test_pick_distribution_matches_ratios():
    cfg = RoutingConfig()
    engine = WeightEngine(routing=cfg)
    engine.update_cache(_all_healthy_signals())
    rng = random.Random(42)
    trials = 10_000
    counts: Counter[tuple[str, str]] = Counter()
    for _ in range(trials):
        c = engine.pick(_three_candidate_tier(), exclude=set(), rng=rng)
        assert c is not None
        counts[(c.provider, c.model)] += 1
    total = sum(counts.values())
    # Per t-1 §14.2: assert total matches trials so a `None` pick (which would
    # also fail the inner `assert c is not None`) can't silently shorten counts.
    assert total == trials
    a = counts[("a", "x")] / total
    b = counts[("b", "y")] / total
    c = counts[("c", "z")] / total
    # Same health/budget => effective weights are proportional to base 50/30/20
    assert abs(a - 0.5) < 0.05
    assert abs(b - 0.3) < 0.05
    assert abs(c - 0.2) < 0.05


def test_pick_excludes_given_set():
    cfg = RoutingConfig()
    engine = WeightEngine(routing=cfg)
    engine.update_cache(_all_healthy_signals())
    excluded = {CandidateRef(provider="a", model="x")}
    rng = random.Random(0)
    for _ in range(50):
        c = engine.pick(_three_candidate_tier(), exclude=excluded, rng=rng)
        assert c is not None
        assert (c.provider, c.model) != ("a", "x")


def test_pick_returns_none_when_all_zero():
    cfg = RoutingConfig()
    engine = WeightEngine(routing=cfg)
    sigs = _all_healthy_signals()
    for k in sigs:
        sigs[k] = CandidateSignals(
            base_weight=0.0,
            error_rate=0.0,
            mean_latency_s=0.5,
            rpm_remaining=100,
            rpm_cap=100,
            tpm_remaining=1000,
            tpm_cap=1000,
            breaker=BreakerState.CLOSED,
        )
    engine.update_cache(sigs)
    assert (
        engine.pick(_three_candidate_tier(), exclude=set(), rng=random.Random(0))
        is None
    )


def test_pick_skips_breaker_open():
    cfg = RoutingConfig()
    engine = WeightEngine(routing=cfg)
    sigs = _all_healthy_signals()
    # make 'a' breaker-open
    sigs[CandidateRef(provider="a", model="x")] = CandidateSignals(
        base_weight=50.0,
        error_rate=0.0,
        mean_latency_s=0.5,
        rpm_remaining=100,
        rpm_cap=100,
        tpm_remaining=1000,
        tpm_cap=1000,
        breaker=BreakerState.OPEN,
    )
    engine.update_cache(sigs)
    rng = random.Random(0)
    chose_a = 0
    for _ in range(200):
        c = engine.pick(_three_candidate_tier(), exclude=set(), rng=rng)
        if c is not None and (c.provider, c.model) == ("a", "x"):
            chose_a += 1
    assert chose_a == 0


def test_pick_missing_signals_treated_as_zero_weight():
    cfg = RoutingConfig()
    engine = WeightEngine(routing=cfg)
    # Cache missing entry for c
    sigs = _all_healthy_signals()
    sigs.pop(CandidateRef(provider="c", model="z"))
    engine.update_cache(sigs)
    rng = random.Random(0)
    for _ in range(100):
        c = engine.pick(_three_candidate_tier(), exclude=set(), rng=rng)
        assert c is not None
        assert (c.provider, c.model) != ("c", "z")


def test_signals_round_trip():
    """Per t-1 §14.7 / §0: replace the `math.isfinite` no-op with a value check.

    Inline math:
      health = (1 - 0.1) * (3 / (3 + 0.5)) = 0.9 * (6/7) ~= 0.7714
      budget = min(50/100, 500/1000) = 0.5
      effective = 10 * 0.7714 * 0.5 ~= 3.857
    """
    s = CandidateSignals(
        base_weight=10.0,
        error_rate=0.1,
        mean_latency_s=0.5,
        rpm_remaining=50,
        rpm_cap=100,
        tpm_remaining=500,
        tpm_cap=1000,
        breaker=BreakerState.CLOSED,
    )
    w = effective_weight(
        base=s.base_weight,
        health=health_score(
            error_rate=s.error_rate,
            mean_latency_s=s.mean_latency_s,
            target_latency_s=3.0,
        ),
        budget=budget_score(
            rpm_remaining=s.rpm_remaining,
            rpm_cap=s.rpm_cap,
            tpm_remaining=s.tpm_remaining,
            tpm_cap=s.tpm_cap,
        ),
        breaker=s.breaker,
        floor=0.02,
    )
    assert w == pytest.approx(3.857, rel=0.01)


# ---------------------------------------------------------------- §14 additions


def test_half_open_candidate_is_pickable():
    """HALF_OPEN is treated like CLOSED at the weights layer.

    `effective_weight` only zeros on `BreakerState.OPEN`. HALF_OPEN
    candidates therefore stay pickable so probes can flow through the
    normal routing path. Documents this design decision (t-1 §14
    Missing scenarios).
    """
    cfg = RoutingConfig()
    engine = WeightEngine(routing=cfg)
    sigs: dict[CandidateRef, CandidateSignals] = {
        CandidateRef(provider="a", model="x"): CandidateSignals(
            base_weight=50.0,
            error_rate=0.0,
            mean_latency_s=0.5,
            rpm_remaining=100,
            rpm_cap=100,
            tpm_remaining=1000,
            tpm_cap=1000,
            breaker=BreakerState.HALF_OPEN,
        ),
        CandidateRef(provider="b", model="y"): CandidateSignals(
            base_weight=0.0,  # zero-weight closed candidate => never picked
            error_rate=0.0,
            mean_latency_s=0.5,
            rpm_remaining=100,
            rpm_cap=100,
            tpm_remaining=1000,
            tpm_cap=1000,
            breaker=BreakerState.CLOSED,
        ),
    }
    engine.update_cache(sigs)
    tier = [
        TierEntry(provider="a", model="x", weight=50.0),
        TierEntry(provider="b", model="y", weight=0.0),
    ]
    rng = random.Random(0)
    picks = {engine.pick(tier, exclude=set(), rng=rng) for _ in range(50)}
    # HALF_OPEN got picked at least once; the zero-weighted closed candidate
    # never wins. Allow a `None` only if cumulative-rounding edge fires; we
    # assert HALF_OPEN was definitely seen.
    assert CandidateRef(provider="a", model="x") in picks
    assert CandidateRef(provider="b", model="y") not in picks


def test_pick_empty_tier_returns_none():
    """`pick([], ...)` short-circuits to None."""
    cfg = RoutingConfig()
    engine = WeightEngine(routing=cfg)
    engine.update_cache(_all_healthy_signals())
    assert engine.pick([], exclude=set(), rng=random.Random(0)) is None


def test_weight_at_floor_is_pickable():
    """When `effective_weight == floor` exactly, the code uses `>= floor`.

    Construct base=0.02, health=1.0, budget=1.0 => w=0.02, floor=0.02.
    `effective_weight` returns 0.02 (kept). The engine should then pick it.
    """
    # Confirm the pure function first.
    w = effective_weight(
        base=0.02,
        health=1.0,
        budget=1.0,
        breaker=BreakerState.CLOSED,
        floor=0.02,
    )
    assert w == pytest.approx(0.02)

    # Now drive through the engine: configure floor=0.02, build signals that
    # produce exactly w=0.02 for the sole candidate.
    cfg = RoutingConfig(min_weight_floor=0.02)
    engine = WeightEngine(routing=cfg)
    cand = CandidateRef(provider="a", model="x")
    engine.update_cache(
        {
            cand: CandidateSignals(
                base_weight=0.02,
                error_rate=0.0,
                mean_latency_s=0.0,  # health = 1.0 * (3 / (3 + 0)) = 1.0
                rpm_remaining=100,
                rpm_cap=100,
                tpm_remaining=1000,
                tpm_cap=1000,
                breaker=BreakerState.CLOSED,
            ),
        }
    )
    tier = [TierEntry(provider="a", model="x", weight=0.02)]
    rng = random.Random(0)
    picked = engine.pick(tier, exclude=set(), rng=rng)
    assert picked == cand
