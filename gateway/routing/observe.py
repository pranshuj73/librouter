"""Sliding-window observation recorder.

Writes per-second hash buckets to Redis (`gw:obs:{p}:{m}:{epoch_sec}`).
Each bucket records:
- `successes`: count
- `failures`: count
- `latency_sum_ms`: integer sum
- `latency_count`: count of latency observations (only on success)
- `fail_<Kind>`: per-error-kind counter

The aggregate window is then summed in `aggregate(candidate)` for use by the
weight engine. Mean latency is `sum / count`. (We use mean rather than p95
here for simplicity; p95 would need T-digest serialization which is overkill
at 20 RPS.)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from gateway.models import CandidateRef
from gateway.redis_state import RedisState


@dataclass(frozen=True, slots=True)
class WindowAggregate:
    successes: int
    failures: int
    total: int
    mean_latency_s: float
    error_rate: float


def default_now_s() -> float:
    return time.time()


class Observer:
    def __init__(
        self,
        *,
        state: RedisState,
        window_s: int = 60,
        now_s_fn: Callable[[], float] = default_now_s,
    ) -> None:
        self._state = state
        self._window_s = window_s
        self._now_s = now_s_fn

    async def record_success(self, cand: CandidateRef, *, latency_s: float) -> None:
        epoch_sec = int(self._now_s())
        key = self._state.observe_key(cand.provider, cand.model, epoch_sec)
        pipe = self._state.client.pipeline(transaction=False)
        pipe.hincrby(key, "successes", 1)
        pipe.hincrby(key, "latency_sum_ms", int(latency_s * 1000))
        pipe.hincrby(key, "latency_count", 1)
        pipe.expire(key, self._window_s * 4)
        await pipe.execute()

    async def record_failure(self, cand: CandidateRef, *, kind: str) -> None:
        epoch_sec = int(self._now_s())
        key = self._state.observe_key(cand.provider, cand.model, epoch_sec)
        pipe = self._state.client.pipeline(transaction=False)
        pipe.hincrby(key, "failures", 1)
        pipe.hincrby(key, f"fail_{kind}", 1)
        pipe.expire(key, self._window_s * 4)
        await pipe.execute()

    async def aggregate(self, cand: CandidateRef) -> WindowAggregate:
        now = int(self._now_s())
        floor = now - self._window_s
        keys = [
            self._state.observe_key(cand.provider, cand.model, sec)
            for sec in range(floor, now + 1)
        ]
        pipe = self._state.client.pipeline(transaction=False)
        for k in keys:
            pipe.hmget(k, "successes", "failures", "latency_sum_ms", "latency_count")
        rows = await pipe.execute()
        successes = failures = latency_sum_ms = latency_count = 0
        for row in rows:
            successes += int(row[0] or 0)
            failures += int(row[1] or 0)
            latency_sum_ms += int(row[2] or 0)
            latency_count += int(row[3] or 0)
        total = successes + failures
        mean_latency_s = (latency_sum_ms / latency_count / 1000.0) if latency_count else 0.0
        error_rate = (failures / total) if total else 0.0
        return WindowAggregate(
            successes=successes,
            failures=failures,
            total=total,
            mean_latency_s=mean_latency_s,
            error_rate=error_rate,
        )
