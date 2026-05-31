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

import math
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
    assert math.isfinite(
        effective_weight(
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
    )
