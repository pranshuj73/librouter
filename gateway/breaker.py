"""Sliding-window circuit breaker per (provider, model), backed by Redis.

Two concerns live here:

1. **Aggregation** — successes and failures per second are recorded into
   Redis sample-counters with a TTL longer than the window. State (closed /
   half-open / open) is derived from the aggregate over the last `window_s`
   seconds.
2. **Hot-path check** — every routing decision needs to know whether to skip a
   candidate. Hitting Redis on every check costs ~0.3ms per attempt and adds
   round-trip latency to the tightest part of the gateway, so each replica
   keeps a per-process snapshot refreshed every ~1s.

Half-open probes are gated by a fleet-wide `SET NX EX` lock so only one
replica probes at a time after the open window expires.

For simplicity in this implementation the sample-counter aggregation also
re-runs inside `refresh_snapshot()`. In production we'd add a Redis pub/sub
subscriber that flips the local snapshot the instant a state transition is
published; the cadence-based refresh remains as a safety net.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from gateway.redis_state import RedisState


class BreakerState(str, Enum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


@dataclass(slots=True)
class _SnapshotEntry:
    state: BreakerState
    opened_at_s: float
    samples: int
    failures: int
    half_opened_at_s: float = 0.0


def default_now_s() -> float:
    return time.monotonic()


class BreakerSet:
    """Per (provider, model) breakers sharing one Redis backend."""

    def __init__(
        self,
        *,
        state: RedisState,
        window_s: float = 30.0,
        min_samples: int = 20,
        open_duration_s: float = 30.0,
        failure_threshold: float = 0.30,
        now_s_fn: Callable[[], float] = default_now_s,
    ) -> None:
        self._state = state
        self._window_s = window_s
        self._min_samples = min_samples
        self._open_duration_s = open_duration_s
        self._failure_threshold = failure_threshold
        self._now_s = now_s_fn
        self._snapshot: dict[tuple[str, str], _SnapshotEntry] = {}

    # ---------------------------------------------------------------- recording

    async def record_success(self, provider: str, model: str) -> None:
        await self._increment(provider, model, "successes")

    async def record_failure(self, provider: str, model: str) -> None:
        await self._increment(provider, model, "failures")

    async def _increment(self, provider: str, model: str, field: str) -> None:
        epoch_sec = int(self._now_s())
        key = self._sample_key(provider, model, epoch_sec)
        r = self._state.client
        pipe = r.pipeline(transaction=False)
        pipe.hincrby(key, field, 1)
        # TTL slightly longer than window so aggregation always sees full window
        pipe.expire(key, int(self._window_s) * 4)
        await pipe.execute()

    def _sample_key(self, provider: str, model: str, epoch_sec: int) -> str:
        return f"gw:brk:{provider}:{model}:samples:{epoch_sec}"

    # ---------------------------------------------------------------- snapshot

    async def refresh_snapshot(self) -> None:
        """Aggregate the last `window_s` seconds of samples for every breaker
        currently in use and update local state.

        #6.1: Build a fresh dict then atomically swap self._snapshot at the
        end.  No lock needed — the swap itself is a single Python assignment
        (atomic at the interpreter level) and avoids torn reads from
        concurrent async paths that may observe a partially-mutated dict.
        """
        now = self._now_s()
        await self._seed_snapshot_from_keys()
        keys_in_window = self._enumerate_sample_keys(now)

        # Start from a copy of the current snapshot so existing state is
        # preserved for candidates not seen in this refresh cycle.
        new_snapshot: dict[tuple[str, str], _SnapshotEntry] = dict(self._snapshot)

        if not keys_in_window:
            # No samples but we may still need to transition open->half_open
            self._transition_after_window_into(new_snapshot, now)
            self._snapshot = new_snapshot
            return

        r = self._state.client
        # Group sample keys by (provider, model)
        per_cand: dict[tuple[str, str], list[str]] = {}
        for cand, sample_key in keys_in_window:
            per_cand.setdefault(cand, []).append(sample_key)

        for cand, sample_keys in per_cand.items():
            pipe = r.pipeline(transaction=False)
            for sk in sorted(sample_keys):
                pipe.hmget(sk, "successes", "failures")
            rows = await pipe.execute()
            successes = sum(int(row[0] or 0) for row in rows)
            failures = sum(int(row[1] or 0) for row in rows)
            total = successes + failures
            entry = self._snapshot.get(cand)
            # For HALF_OPEN, also count samples strictly after the half-open
            # transition so a single probe result decides the next state.
            post_half_open: tuple[int, int] | None = None
            if entry and entry.state is BreakerState.HALF_OPEN:
                ho_sec = int(entry.half_opened_at_s)
                ho_rows = []
                pipe2 = r.pipeline(transaction=False)
                ho_keys = [
                    self._sample_key(cand[0], cand[1], sec)
                    for sec in range(ho_sec, int(now) + 1)
                ]
                for sk in ho_keys:
                    pipe2.hmget(sk, "successes", "failures")
                ho_rows = await pipe2.execute()
                ho_succ = sum(int(row[0] or 0) for row in ho_rows)
                ho_fail = sum(int(row[1] or 0) for row in ho_rows)
                post_half_open = (ho_succ, ho_fail)
            new_entry = self._compute_next_state(
                entry, total, failures, now, post_half_open
            )
            new_snapshot[cand] = new_entry

        self._transition_after_window_into(new_snapshot, now)
        # Atomic swap — readers see either the old complete dict or the new one.
        self._snapshot = new_snapshot

    def _enumerate_sample_keys(self, now: float) -> list[tuple[tuple[str, str], str]]:
        """List sample keys per (provider, model) that we've ever recorded."""
        # The snapshot dict tells us which candidates are in play; we also want
        # newly-seen ones. We do a SCAN once per refresh — cheap at our scale.
        # For test purposes (and fake-redis), a SCAN over `gw:brk:*:samples:*`
        # walks every sample key.
        # NOTE: at production scale we'd track seen candidates separately to
        # avoid the SCAN cost (~5ms with thousands of keys), but at 20 RPS
        # the key cardinality is small.
        seen: list[tuple[tuple[str, str], str]] = []
        # We can't `await` inside a sync method; this method only assembles
        # keys from the known snapshot + sample-window math. The SCAN happens
        # in `_scan_keys` (async).
        floor = int(now - self._window_s)
        ceiling = int(now)
        for cand in list(self._snapshot.keys()):
            for sec in range(floor, ceiling + 1):
                seen.append((cand, self._sample_key(cand[0], cand[1], sec)))
        return seen

    async def _seed_snapshot_from_keys(self) -> None:
        """One-time SCAN to pick up candidates that have samples but no
        snapshot entry yet."""
        r = self._state.client
        prefix = "gw:brk:"
        async for raw_key in r.scan_iter(match=f"{prefix}*:samples:*", count=200):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            rest = key[len(prefix):]
            # rest = "{provider}:{model}:samples:{epoch_sec}"
            parts = rest.split(":samples:")
            if len(parts) != 2:
                continue
            pm = parts[0].split(":", 1)
            if len(pm) != 2:
                continue
            cand = (pm[0], pm[1])
            self._snapshot.setdefault(
                cand,
                _SnapshotEntry(
                    state=BreakerState.CLOSED,
                    opened_at_s=0.0,
                    samples=0,
                    failures=0,
                ),
            )

    def _compute_next_state(
        self,
        prev: _SnapshotEntry | None,
        total: int,
        failures: int,
        now: float,
        post_half_open: tuple[int, int] | None = None,
    ) -> _SnapshotEntry:
        cur_state = prev.state if prev else BreakerState.CLOSED
        opened_at = prev.opened_at_s if prev else 0.0
        half_opened_at = prev.half_opened_at_s if prev else 0.0

        # If currently OPEN, hold OPEN — the time-driven transition into
        # HALF_OPEN runs in `_transition_after_window`.
        if cur_state is BreakerState.OPEN:
            return _SnapshotEntry(
                state=BreakerState.OPEN,
                opened_at_s=opened_at,
                samples=total,
                failures=failures,
                half_opened_at_s=half_opened_at,
            )

        # HALF_OPEN: first new sample since transition is the probe result.
        if cur_state is BreakerState.HALF_OPEN:
            if post_half_open is not None:
                ho_succ, ho_fail = post_half_open
                if ho_fail > 0:
                    return _SnapshotEntry(
                        state=BreakerState.OPEN,
                        opened_at_s=now,
                        samples=total,
                        failures=failures,
                        half_opened_at_s=0.0,
                    )
                if ho_succ > 0:
                    return _SnapshotEntry(
                        state=BreakerState.CLOSED,
                        opened_at_s=0.0,
                        samples=total,
                        failures=failures,
                        half_opened_at_s=0.0,
                    )
            return _SnapshotEntry(
                state=BreakerState.HALF_OPEN,
                opened_at_s=opened_at,
                samples=total,
                failures=failures,
                half_opened_at_s=half_opened_at,
            )

        # CLOSED: trip if threshold exceeded with enough samples.
        if total >= self._min_samples and failures / total >= self._failure_threshold:
            return _SnapshotEntry(
                state=BreakerState.OPEN,
                opened_at_s=now,
                samples=total,
                failures=failures,
            )
        return _SnapshotEntry(
            state=BreakerState.CLOSED,
            opened_at_s=0.0,
            samples=total,
            failures=failures,
        )

    def _transition_after_window_into(
        self, snapshot: dict[tuple[str, str], _SnapshotEntry], now: float
    ) -> None:
        """Mutate *snapshot* in-place: OPEN -> HALF_OPEN after open_duration_s."""
        for cand, entry in list(snapshot.items()):
            if (
                entry.state is BreakerState.OPEN
                and now - entry.opened_at_s >= self._open_duration_s
            ):
                snapshot[cand] = _SnapshotEntry(
                    state=BreakerState.HALF_OPEN,
                    opened_at_s=entry.opened_at_s,
                    samples=entry.samples,
                    failures=entry.failures,
                    half_opened_at_s=now,
                )

    # ---------------------------------------------------------------- queries

    async def state(self, provider: str, model: str) -> BreakerState:
        await self._seed_snapshot_from_keys()
        entry = self._snapshot.get((provider, model))
        if entry is None:
            return BreakerState.CLOSED
        return entry.state

    async def snapshot(self) -> dict[tuple[str, str], _SnapshotEntry]:
        return dict(self._snapshot)

    # ---------------------------------------------------------------- probing

    async def try_probe(self, provider: str, model: str) -> bool:
        """Acquire the fleet-wide probe slot. Returns True if this replica is
        permitted to send the single half-open probe."""
        key = self._state.breaker_probe_key(provider, model)
        acquired = await self._state.acquire_probe_lock(
            key, holder=f"probe:{int(self._now_s())}", ttl_s=10
        )
        return acquired
