"""Background refresh that turns Redis-side rolling stats + bucket state +
breaker snapshot into a `CandidateSignals` map for the WeightEngine.

The main loop (`RefreshTask.run`) calls `build_signals` every
`refresh_interval_ms` and updates the engine cache in place.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable

from gateway.breaker import BreakerSet, BreakerState
from gateway.metrics import REFRESH_ERRORS_TOTAL
from gateway.models import CandidateRef, Config
from gateway.ratelimit import RedisTokenBucket
from gateway.routing.observe import Observer
from gateway.routing.weights import CandidateSignals, WeightEngine


log = logging.getLogger(__name__)


def _all_candidates(cfg: Config) -> Iterable[tuple[CandidateRef, float, "RateLimitEntry"]]:
    """Yield every (candidate_ref, base_weight, rate_limits) across all tiers, deduplicated.

    If the same (provider, model) appears in multiple tiers with different
    base weights, the first occurrence wins. This is a config smell — we'd
    typically reject it in models.py — but we tolerate it here so the
    refresh task can't blow up at runtime on configs that snuck through.
    """
    from gateway.models import RateLimitEntry  # local import avoids circular at module level

    seen: dict[CandidateRef, tuple[float, RateLimitEntry]] = {}
    for tier_cfg in cfg.tiers.values():
        for t in tier_cfg.candidates:
            ref = CandidateRef(provider=t.provider, model=t.model)
            seen.setdefault(ref, (t.weight, t.rate_limits))
    return ((ref, weight, rl) for ref, (weight, rl) in seen.items())


async def build_signals(
    cfg: Config,
    observer: Observer,
    bucket: RedisTokenBucket,
    breakers: BreakerSet,
    *,
    available_providers: set[str] | None = None,
) -> dict[CandidateRef, CandidateSignals]:
    """Compute one snapshot of signals across every candidate.

    If `available_providers` is given, candidates whose provider isn't in the
    set are excluded from the snapshot. The weight engine treats missing
    candidates as weight 0, so the router won't pick them.
    """
    out: dict[CandidateRef, CandidateSignals] = {}
    # Make sure the breaker snapshot is up to date before reading state.
    await breakers.refresh_snapshot()

    for cand, base_weight, rl in _all_candidates(cfg):
        if available_providers is not None and cand.provider not in available_providers:
            continue

        agg = await observer.aggregate(cand)
        rpm_remaining, tpm_remaining = await bucket.remaining(cand.provider, cand.model)
        brk = await breakers.state(cand.provider, cand.model)

        out[cand] = CandidateSignals(
            base_weight=base_weight,
            error_rate=agg.error_rate,
            mean_latency_s=agg.mean_latency_s,
            rpm_remaining=rpm_remaining,
            rpm_cap=rl.rpm,
            tpm_remaining=tpm_remaining,
            tpm_cap=rl.tpm,
            breaker=brk if isinstance(brk, BreakerState) else BreakerState.CLOSED,
        )
    return out


class RefreshTask:
    """Periodic refresher. Owns the asyncio task lifecycle."""

    def __init__(
        self,
        *,
        config: Config,
        observer: Observer,
        bucket: RedisTokenBucket,
        breakers: BreakerSet,
        engine: WeightEngine,
        available_providers: set[str] | None = None,
    ) -> None:
        self._cfg = config
        self._obs = observer
        self._bk = bucket
        self._br = breakers
        self._engine = engine
        self._available_providers = available_providers
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def tick(self) -> None:
        signals = await build_signals(
            self._cfg,
            self._obs,
            self._bk,
            self._br,
            available_providers=self._available_providers,
        )
        self._engine.update_cache(signals)

    async def _loop(self) -> None:
        # #6.2: jittered exponential backoff on consecutive failures so a
        # Redis outage doesn't produce many log lines and metric increments
        # per second. The base interval is restored after the first success.
        base_interval_s = self._cfg.routing.refresh_interval_ms / 1000.0
        max_backoff_s = 30.0
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                await self.tick()
                consecutive_failures = 0
                next_wait_s = base_interval_s
            except Exception:
                consecutive_failures += 1
                REFRESH_ERRORS_TOTAL.inc()
                if consecutive_failures == 1:
                    log.exception("refresh tick failed")
                backoff = min(
                    max_backoff_s,
                    base_interval_s * (2 ** (consecutive_failures - 1)),
                )
                # 50–100% of nominal backoff so concurrent replicas don't sync.
                next_wait_s = backoff * (0.5 + random.random() * 0.5)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=next_wait_s)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="routing-refresh")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
