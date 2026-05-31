"""Provider-autorouting weight engine.

Each request's candidate is picked by weighted-random selection over the
non-excluded, non-breaker-open candidates in the tier. Weights are not the
base values from config — they're recomputed per refresh from base × health
score × budget score.

The cache is updated externally by `routing/refresh.py` once per second; the
hot path only reads `_cache` and never blocks on Redis.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from gateway.breaker import BreakerState
from gateway.models import CandidateRef, RoutingConfig, TierEntry


@dataclass(frozen=True, slots=True)
class CandidateSignals:
    """Latest known signals for one (provider, model). Fed by refresh.py."""

    base_weight: float
    error_rate: float
    mean_latency_s: float
    rpm_remaining: int
    rpm_cap: int
    tpm_remaining: int
    tpm_cap: int
    breaker: BreakerState


def health_score(
    *, error_rate: float, mean_latency_s: float, target_latency_s: float
) -> float:
    """In [0, 1]. Healthy → near 1. Bad → near 0.

    `(1 - error_rate)` zeros on full failure. The latency term
    `target / (target + observed)` softly degrades over the target.
    """
    error_factor = max(0.0, 1.0 - error_rate)
    latency_factor = target_latency_s / (target_latency_s + max(0.0, mean_latency_s))
    return error_factor * latency_factor


def budget_score(
    *, rpm_remaining: int, rpm_cap: int, tpm_remaining: int, tpm_cap: int
) -> float:
    if rpm_cap <= 0 or tpm_cap <= 0:
        return 0.0
    rpm = max(0.0, rpm_remaining / rpm_cap)
    tpm = max(0.0, tpm_remaining / tpm_cap)
    return min(rpm, tpm)


def effective_weight(
    *,
    base: float,
    health: float,
    budget: float,
    breaker: BreakerState,
    floor: float,
) -> float:
    if breaker is BreakerState.OPEN:
        return 0.0
    w = base * health * budget
    return w if w >= floor else 0.0


class WeightEngine:
    """Per-replica weight engine. Hot-path-safe (no I/O in `pick`)."""

    def __init__(self, *, routing: RoutingConfig) -> None:
        self._routing = routing
        self._cache: dict[CandidateRef, CandidateSignals] = {}

    def update_cache(self, signals: dict[CandidateRef, CandidateSignals]) -> None:
        # Atomic replace — refresh.py builds a complete new map per tick.
        self._cache = dict(signals)

    def signals_for(self, cand: CandidateRef) -> CandidateSignals | None:
        return self._cache.get(cand)

    def _weight(self, cand: CandidateRef) -> float:
        s = self._cache.get(cand)
        if s is None:
            return 0.0
        h = health_score(
            error_rate=s.error_rate,
            mean_latency_s=s.mean_latency_s,
            target_latency_s=self._routing.target_latency_s,
        )
        b = budget_score(
            rpm_remaining=s.rpm_remaining,
            rpm_cap=s.rpm_cap,
            tpm_remaining=s.tpm_remaining,
            tpm_cap=s.tpm_cap,
        )
        return effective_weight(
            base=s.base_weight,
            health=h,
            budget=b,
            breaker=s.breaker,
            floor=self._routing.min_weight_floor,
        )

    def pick(
        self,
        tier_candidates: list[TierEntry],
        exclude: set[CandidateRef],
        rng: random.Random,
    ) -> CandidateRef | None:
        cands: list[CandidateRef] = []
        weights: list[float] = []
        for t in tier_candidates:
            ref = CandidateRef(provider=t.provider, model=t.model)
            if ref in exclude:
                continue
            w = self._weight(ref)
            if w <= 0.0:
                continue
            cands.append(ref)
            weights.append(w)
        if not cands:
            return None
        total = sum(weights)
        r = rng.random() * total
        acc = 0.0
        for c, w in zip(cands, weights, strict=True):
            acc += w
            if r <= acc:
                return c
        return cands[-1]
