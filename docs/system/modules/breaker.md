# `gateway/breaker.py` — Sliding-window circuit breaker

## Purpose

Per `(provider, model)` circuit breaker with a sliding-window error-ratio trigger, a time-based OPEN duration, and a fleet-wide half-open probe. Two concerns live in this module:

1. **Aggregation** — every success and failure is recorded into a Redis hash bucketed by epoch second (`gw:brk:{p}:{m}:samples:{epoch_sec}`). The sliding window is the trailing `window_s` seconds of these per-second hashes. TTL on each hash is `window_s * 4`, long enough that aggregation always sees the full window even with clock jitter.
2. **Hot-path query** — `Router` calls into the breaker on every routing decision (transitively, via `WeightEngine`'s `CandidateSignals.breaker` field — see [`router.md`](router.md)). Hitting Redis per attempt is too expensive (~0.3 ms each), so the breaker maintains an in-process snapshot dict, refreshed by the `RefreshTask` at ~1 Hz. Reads from the snapshot are O(1) and never block on I/O.

The module docstring (`breaker.py:18-21`) describes a future Redis pub/sub fast-path that flips the local snapshot the instant a transition is published. **That path is not implemented.** See "Open questions" below.

## Public surface

Importable from `gateway.breaker`:

| Symbol | Type | Notes |
|---|---|---|
| `BreakerState` | `Enum[str]` | `CLOSED`, `HALF_OPEN`, `OPEN`. |
| `BreakerSet` | `class` | Per-`(provider, model)` breakers sharing one Redis backend. |
| `BreakerSet(state, window_s=30.0, min_samples=20, open_duration_s=30.0, failure_threshold=0.30, now_s_fn=default_now_s)` | constructor | See [Configuration knobs](#configuration-knobs). |
| `BreakerSet.record_success(provider, model)` | `async -> None` | `HINCRBY ... successes 1 ⇢ EXPIRE`. |
| `BreakerSet.record_failure(provider, model)` | `async -> None` | `HINCRBY ... failures 1 ⇢ EXPIRE`. |
| `BreakerSet.state(provider, model)` | `async -> BreakerState` | Reads from in-process snapshot; seeds it via one `SCAN` if empty. |
| `BreakerSet.snapshot()` | `async -> dict[tuple[str, str], _SnapshotEntry]` | Copy of the current snapshot. |
| `BreakerSet.refresh_snapshot()` | `async -> None` | Rebuilds the snapshot from Redis. Called by `RefreshTask.tick`. |
| `BreakerSet.try_probe(provider, model)` | `async -> bool` | `True` iff this replica wins the fleet-wide probe lock. |
| `default_now_s()` | `() -> float` | `time.monotonic()`. Injectable. |

`_SnapshotEntry` is a private slots dataclass with fields `state: BreakerState`, `opened_at_s: float`, `samples: int`, `failures: int`, `half_opened_at_s: float = 0.0`. It is exposed via `snapshot()` for tests and metrics scraping.

## State machine

```
                         failure_ratio >= failure_threshold
                              AND samples >= min_samples
                  ┌─────────────────────────────────────────┐
                  ▼                                         │
        ┌───────────────────┐                    ┌──────────┴────────┐
        │      CLOSED       │                    │       OPEN        │
        │  pickable, normal │                    │ excluded; weight=0│
        └────────┬──────────┘                    └──────────┬────────┘
                 ▲                                          │
                 │ probe success                            │ now - opened_at_s >= open_duration_s
                 │ (ho_succ > 0)                            ▼
        ┌────────┴──────────┐  probe failure   ┌────────────────────┐
        │     HALF_OPEN     │◄─────────────────┤ time-driven; in    │
        │  one probe in     │  (ho_fail > 0)   │ _transition_after_ │
        │  flight allowed   │                  │ window_into        │
        └───────────────────┘                  └────────────────────┘
```

Transitions in tabular form:

| From | To | Trigger | Where |
|---|---|---|---|
| CLOSED | OPEN | `total >= min_samples and failures/total >= failure_threshold` | `_compute_next_state` (`breaker.py:258`) |
| OPEN | HALF_OPEN | `now - opened_at_s >= open_duration_s` | `_transition_after_window_into` (`breaker.py:272-287`) |
| HALF_OPEN | CLOSED | `post_half_open` shows `ho_succ > 0` and no failures since transition | `_compute_next_state` (`breaker.py:241-248`) |
| HALF_OPEN | OPEN | `post_half_open` shows `ho_fail > 0` | `_compute_next_state` (`breaker.py:233-240`) |

Notes:

- The CLOSED → OPEN transition requires **both** the threshold and `min_samples`. A few failures at low traffic don't trip the breaker.
- The OPEN → HALF_OPEN transition is purely time-based and runs on every `refresh_snapshot()` tick, regardless of new samples.
- HALF_OPEN counts samples **strictly after** `half_opened_at_s` (`breaker.py:137-150`), so a single probe result determines the next state — old samples from before the OPEN window don't pollute the decision.

## Internals

### Sample-hash layout

```python
# breaker.py:91-92
def _sample_key(self, provider, model, epoch_sec):
    return f"gw:brk:{provider}:{model}:samples:{epoch_sec}"
```

| Field | Type | Owner |
|---|---|---|
| `successes` | counter | `record_success` |
| `failures` | counter | `record_failure` |

TTL is `window_s * 4` seconds (`breaker.py:88`), set on every increment. That's 120s for the default 30s window — comfortably wider than the window so a full sliding window's worth of hashes is always present even with clock drift, GC pauses, or refresh lag.

`_increment` issues a non-transactional pipeline:

```python
# breaker.py:84-89
pipe = r.pipeline(transaction=False)
pipe.hincrby(key, field, 1)
pipe.expire(key, int(self._window_s) * 4)
await pipe.execute()
```

The two commands aren't atomic with each other, but it doesn't matter: `HINCRBY` creates the key if absent; a missed `EXPIRE` would just mean the next increment sets it. The worst case is a sample hash without a TTL, which is corrected on the next sample for the same second.

### Snapshot — in-process cache

`BreakerSet._snapshot: dict[tuple[str, str], _SnapshotEntry]` is the hot-path read store. The `RefreshTask` rebuilds it via `refresh_snapshot()` every `refresh_interval_ms` (~1 s).

The rebuild algorithm (`breaker.py:96-158`) — cr-1 §6.1 fix in commit `8e046e5` reshaped this around an explicit local-then-swap pattern:

1. `_seed_snapshot_from_keys()` — `SCAN gw:brk:*:samples:*` to pick up `(provider, model)` candidates that have samples in Redis but no snapshot entry yet (e.g. another replica recorded them; this replica hasn't seen them).
2. Build a fresh `new_snapshot = dict(self._snapshot)` (`breaker.py:111`) — every mutation that follows targets the local dict, never `self._snapshot`.
3. Enumerate every per-second sample key in `[now - window_s, now]` for every known candidate.
4. For each candidate, pipeline `HMGET successes failures` across its sample keys; sum.
5. For HALF_OPEN candidates only, do a second pipeline restricted to seconds `>= half_opened_at_s` — the "post-half-open" view that decides the probe result.
6. `_compute_next_state(prev, total, failures, now, post_half_open)` returns a fresh `_SnapshotEntry`; write it to `new_snapshot[cand]`.
7. `_transition_after_window_into(new_snapshot, now)` promotes OPEN candidates past their open window to HALF_OPEN — also mutating only the local dict.
8. **Atomic swap**: `self._snapshot = new_snapshot` (`breaker.py:158`). Python attribute assignment is a single bytecode op, so concurrent readers see either the old complete dict or the new one — never a torn read mid-iteration.

```python
# breaker.py:111
new_snapshot: dict[tuple[str, str], _SnapshotEntry] = dict(self._snapshot)
...
# breaker.py:156-158
self._transition_after_window_into(new_snapshot, now)
# Atomic swap — readers see either the old complete dict or the new one.
self._snapshot = new_snapshot
```

The no-samples short-circuit at `breaker.py:113-117` also applies the post-window transition to `new_snapshot` and then swaps, so even the empty path obeys the rebuild-then-swap discipline.

### Residual concurrency caveat — `_seed_snapshot_from_keys` still mutates `self._snapshot`

The cr-1 §6.1 atomic-swap fix (commit `8e046e5`) covers the main rebuild path but `_seed_snapshot_from_keys()` (`breaker.py:180-204`) still mutates `self._snapshot` directly via `setdefault(...)` before the atomic swap. If two coroutines call `refresh_snapshot()` concurrently — or `state()` calls `_seed_snapshot_from_keys()` while `refresh_snapshot()` is mid-flight — the seed phase can interleave on the same dict without a lock.

In practice the only caller of `refresh_snapshot` is `RefreshTask.tick`, which serializes its own invocations, and `state()` is also called from `RefreshTask` (via `build_signals` — `routing/refresh.py:66`). So today the contention window is narrow. The remaining fix is to lift the seed into a local dict and merge it into `new_snapshot` before the swap, or guard with an `asyncio.Lock`. Tracked in [Open questions](#open-questions--known-gaps).

### `try_probe` — fleet-wide half-open gate

```python
# breaker.py:303-310
async def try_probe(self, provider, model):
    key = self._state.breaker_probe_key(provider, model)
    acquired = await self._state.acquire_probe_lock(
        key, holder=f"probe:{int(self._now_s())}", ttl_s=10
    )
    return acquired
```

This is `SET gw:brk:{p}:{m}:probe <holder> NX EX 10` under the hood (see [`redis-state.md`](redis-state.md#lua-script-3--probe-lock-no-lua-plain-set-nx-ex)). The semantics:

- Exactly one replica wins per `(provider, model)` per 10s window.
- The winner is permitted to send the single half-open probe (in practice, the next routed attempt for that candidate becomes the probe).
- The TTL (10 s) bounds the consequence of the holder crashing before the probe resolves: at most 10 s of probe blackout, after which any replica can re-acquire.
- There is no release path — holders don't `DEL` the key on success or failure. The lock auto-expires. This is intentional: a `DEL` race would require ownership verification (CAS), and 10s of blackout is acceptable.

The probe-result handling is **not** in `try_probe` — it falls out of normal request handling. The winning replica routes the next request through that candidate; the success/failure is recorded via `record_success`/`record_failure` like any other attempt; the next `refresh_snapshot` reads the post-half-open samples and flips the state.

## Concurrency model

- **Atomic** (Redis primitive): probe lock via `SET NX EX` — fleet-wide at-most-one probe per 10 s.
- **Atomic** (Python bytecode): the snapshot swap `self._snapshot = new_snapshot` (`breaker.py:158`). The full rebuild — copy of the previous snapshot, per-candidate `_compute_next_state` writes, and the `_transition_after_window_into` mutation — all target the local `new_snapshot` dict; readers in the snapshot are guaranteed to see a complete state.
- **Atomic** (Redis serialization): each `HINCRBY` and each `EXPIRE`. The two are not atomic *together*, but the missed-TTL case is self-healing.
- **NOT atomic**: the read-modify-write across `_seed_snapshot_from_keys` → `_compute_next_state` → swap. The reads are pipelined but not transactional. New samples landing mid-refresh are seen on the next tick.
- **NOT atomic**: concurrent invocations of `_seed_snapshot_from_keys()` against `self._snapshot` (see "Residual concurrency caveat" above). The main rebuild path is safe (cr-1 §6.1 resolved in commit `8e046e5`); the seeding leg is the remaining race.
- **`fakeredis` covers** the per-process serialization of every Redis command and the atomicity of `SET NX EX` (see t-1 §4.5, §8.5). **Real Redis** is required to validate cross-replica probe-lock atomicity; no integration test exists today (t-1 §8.5).

### Probe-lock at-most-once guarantee

The contract is **at-most-one probe per `(provider, model)` per `ttl_s` window across the fleet**. Combined with the breaker rule "any sample after `half_opened_at_s` decides the next state", this gives an effectively-once probing semantics during an outage: the winning replica's probe result is the one that flips HALF_OPEN to CLOSED or back to OPEN; losers see HALF_OPEN until the next refresh and either skip the candidate (their `state()` query returned HALF_OPEN before the lock) or get NX-rejected if they tried `try_probe`.

If the holder dies before the probe resolves, the 10 s TTL releases the lock and any replica can re-attempt. The breaker state remains HALF_OPEN — the dead probe contributed neither a success nor a failure sample — and the next probe attempt is just another HALF_OPEN entry.

## Failure modes

| Failure | Behavior |
|---|---|
| Redis unreachable on `record_*` | The `redis.asyncio` pipeline raises. Caller in `Router.route` does not handle it; HTTP 500. |
| Redis unreachable on `refresh_snapshot` | `RefreshTask._loop` catches via `log.exception("refresh tick failed")` (`routing/refresh.py:117-119`) and retries on next tick. The snapshot goes stale but the gateway keeps serving from the last-known state. |
| Redis unreachable on `try_probe` | Raises; propagates. Today `try_probe` is not yet wired into the router's hot path — the breaker state-machine sees the next HALF_OPEN attempt as the probe automatically. |
| Snapshot stale (refresh failing repeatedly) | Routing decisions use the last-good snapshot indefinitely. A vendor that recovered will not be re-admitted until refresh succeeds. A vendor that just started failing keeps getting traffic for a few seconds until OPEN propagates. Acceptable degradation. |
| Probe-lock holder dies | 10 s TTL releases. Next refresh allows another replica to try. |
| Clock skew on `now_s_fn` | `time.monotonic()` is monotonic by definition. If a test injects a non-monotonic clock and time goes backwards: the OPEN → HALF_OPEN gate (`now - opened_at_s >= open_duration_s`) might never trip if `now < opened_at_s`. Tests should inject monotonic-only clocks. |
| Sample-hash TTL expires mid-window | The hash is just absent on read; `HMGET` returns `[nil, nil]`, treated as `(0, 0)`. With `window_s * 4` TTL, this only happens after `4 * window_s` of zero traffic to that candidate, by which point the aggregate is also zero and the breaker is CLOSED. Self-consistent. |
| `record_failure` + `EXPIRE` partial — TTL set without increment, or increment without TTL | Self-healing on next sample for the same second. |
| Snapshot concurrent mutation (cr-1 §6.1 main path) | Resolved in commit `8e046e5` — `refresh_snapshot` builds `new_snapshot` locally and swaps in one statement. The seed leg (`_seed_snapshot_from_keys`) still mutates `self._snapshot` directly; benign while only `RefreshTask` invokes it. |

## Configuration knobs

Constructor args on `BreakerSet`:

| Knob | Default | Meaning |
|---|---|---|
| `state` | required | The `RedisState`. |
| `window_s` | `30.0` | Sliding-window width. Sample-hash TTL is `4 * window_s`. |
| `min_samples` | `20` | Minimum total samples in window before threshold can trip. Stops noise tripping at low traffic. |
| `open_duration_s` | `30.0` | How long OPEN persists before time-based HALF_OPEN promotion. |
| `failure_threshold` | `0.30` | `failures / total >= this` trips CLOSED → OPEN. |
| `now_s_fn` | `default_now_s` (= `time.monotonic()`) | Test seam. |

There are no env vars read by this module. None of these knobs are surfaced through `Config` today — they are constructed in `gateway/app.py` with the defaults above. If they need to become configurable, add a `BreakerConfig` to `gateway/models.py`.

`try_probe` hard-codes `ttl_s=10` (`breaker.py:309`). Not currently configurable.

## Open questions / known gaps

- **cr-1 §6.1 atomic snapshot rebuild — Resolved in commit `8e046e5`.** `refresh_snapshot` now builds `new_snapshot` locally and swaps with a single `self._snapshot = new_snapshot` assignment (`breaker.py:111, 158`); readers can no longer observe a partially-mutated dict from the main rebuild path.
- **Pub/sub for state transitions is unimplemented.** The module docstring (`breaker.py:18-21`) describes a planned Redis pub/sub subscriber that flips local snapshots on transition events; `CHANNEL_BREAKER` is declared in [`redis-state.md`](redis-state.md#key-layout) for it; **commit `8e046e5` did not add a publisher or subscriber**. Today the only path from a remote replica's transition to this replica's view is the next `refresh_snapshot` tick. See t-1 §4 missing scenarios.
- **Seed leg still mutates `self._snapshot`.** `_seed_snapshot_from_keys()` writes to `self._snapshot` directly via `setdefault`. Benign today because only `RefreshTask` calls `state()` and `refresh_snapshot()`, but a latent concurrency bug. Fix: seed into a local dict and merge into `new_snapshot` before the swap.
- **`try_probe` is not wired into the hot path.** The current hot path simply lets the next HALF_OPEN attempt act as the probe — the lock primitive exists but no caller invokes it. If two replicas independently route to a HALF_OPEN candidate in the same second, both attempts contribute to `post_half_open` and the first sample wins.
- **No release for the probe lock.** Acceptable but means HALF_OPEN can be sticky for up to 10 s after a fast resolution.
- **Knobs are hard-coded.** `window_s`, `failure_threshold`, etc. live in `gateway/app.py` construction; not in YAML. Operators can't tune without a code change.
- **`SCAN` cost.** `_seed_snapshot_from_keys()` walks every `gw:brk:*:samples:*` key every refresh tick. The code comment (`breaker.py:166-168`) notes this is acceptable "at 20 RPS" but degrades at production scale. Fix: track seen candidates in a Redis set, or rely on the config-derived candidate list.
- **No integration test against real Redis** for the breaker (t-1 §8.5).
- **No test covering the `_seed_snapshot_from_keys` race** with `refresh_snapshot`.
- **`KEY_BREAKER` is declared but unused.** The "current state" is in-process only; there is no Redis-side authoritative state key.

## Interaction with the routing layer

The breaker contributes to routing decisions through two paths:

1. **Direct state read** during `build_signals` (`routing/refresh.py:66`): `await breakers.state(provider, model)` returns the current `BreakerState`, which lands in `CandidateSignals.breaker`. The `WeightEngine` collapses the candidate's effective weight to zero when `breaker is BreakerState.OPEN` — see [`router.md`](router.md) for the weight formula.
2. **Indirect via attempt outcomes**: every successful `vendor.chat` call leads to `record_success`; every failed one to `record_failure`. The next `refresh_snapshot` tick aggregates them into a fresh `_SnapshotEntry`.

The full feedback loop:

```
Router.route
   ─→ vendor.chat (success or ProviderError)
       ⇢ BreakerSet.record_success / record_failure  (fire-and-forget from the router's POV; awaited but doesn't gate the response)
            → Redis HINCRBY ... successes|failures 1 ⇢ EXPIRE
┄
RefreshTask._loop (every refresh_interval_ms)
   ─→ BreakerSet.refresh_snapshot
       ─→ SCAN (one-time seeding) → HMGET pipelines per candidate → _compute_next_state → atomic swap
   ─→ build_signals → CandidateSignals.breaker = BreakerSet.state(...)
   ─→ WeightEngine.update_cache(signals)
┄
Router.route (next request)
   ─→ WeightEngine.pick(...)  ← cached, in-process, no I/O
       ← skips OPEN candidates entirely
```

The breaker's effect on routing is therefore lagged by one refresh tick (~1 s default). This is acceptable for sliding-window-driven trips at the configured `min_samples=20` / `failure_threshold=0.30` — a vendor that suddenly starts failing produces ~20 samples worth of mis-routed traffic before being excluded. Tighter recovery would require the unimplemented pub/sub fast-path or a synchronous state check on every routing decision (more I/O).

## Metrics surface

The breaker does not export Prometheus metrics directly from this module. The `observability` module reads `BreakerSet.snapshot()` periodically to populate a gauge per `(provider, model)` labeled with `state`. State transitions are not currently logged at WARNING — they show up only in the Prometheus gauge. Operator dashboards should alert on `breaker_state == "open"` for longer than `open_duration_s + refresh_interval_ms` to detect persistent outages.

## Testing notes

`tests/test_breaker.py` (owned by another agent) covers:

- CLOSED → OPEN on enough failure samples.
- OPEN → HALF_OPEN after `open_duration_s` (via injected clock).
- HALF_OPEN → CLOSED on a probe success.
- HALF_OPEN → OPEN on a probe failure.
- `try_probe` returns `False` on the second concurrent call (NX semantics).
- Snapshot is preserved for candidates not seen in a refresh cycle.

Not yet covered (t-1 §4, §8):

- Cross-replica probe-lock atomicity against real Redis.
- The `_seed_snapshot_from_keys` race with concurrent `refresh_snapshot`.
- The `RefreshTask._loop` exception path that catches `refresh_snapshot` failures.
- Stale-snapshot behavior when Redis is unreachable mid-flight.
- Non-monotonic injected clock (HALF_OPEN promotion gate).
