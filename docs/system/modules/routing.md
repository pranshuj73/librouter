# gateway/routing/ — WeightEngine, Observer, RefreshTask

## Purpose

The `gateway/routing/` package is the brain of the gateway's load-balancing decision. Three pieces work together:

- **`WeightEngine`** (`gateway/routing/weights.py`) — the *hot-path read*. Owns an in-process cache of per-`(provider, model)` signals and answers `pick(tier_candidates, exclude, rng)` in microseconds without touching Redis.
- **`Observer`** (`gateway/routing/observe.py`) — the *hot-path write*. Each attempt the router runs ends in a fire-and-forget call to `Observer.record_success` or `record_failure`, which `HINCRBY`s a per-second hash bucket in Redis.
- **`RefreshTask`** (`gateway/routing/refresh.py`) — the *background reconciler*. Once per `routing.refresh_interval_ms` (default 1000 ms) it aggregates Observer windows, polls bucket remainings, polls breaker state, and atomically swaps the `WeightEngine._cache`. On consecutive tick failures it backs off exponentially (see [Internals](#refreshtask--the-background-reconciler)).

Together they implement the separation called out in [`../architecture.md`](../architecture.md) §5: the hot path is read-only and synchronous; the slow path is asynchronous and writes to a cache the hot path reads from. The router never blocks on the routing subsystem's network calls.

The Redis backplane these three share — keys, pipelines, Lua scripts — lives in [`redis-state.md`](redis-state.md).

## Public surface

### `weights.py`

| Symbol | Type | Purpose |
|---|---|---|
| `WeightEngine` | class | Per-replica weight cache; sole entry point for routing decisions. |
| `WeightEngine.__init__(*, routing: RoutingConfig)` | constructor | Captures `target_latency_s` and `min_weight_floor` from config. |
| `WeightEngine.update_cache(signals: dict[CandidateRef, CandidateSignals])` | method | Atomic swap. Called by `RefreshTask`. |
| `WeightEngine.signals_for(cand: CandidateRef) -> CandidateSignals \| None` | method | Snapshot read; used by `/metrics` gauge refresh. |
| `WeightEngine.pick(tier_candidates, exclude, rng) -> CandidateRef \| None` | method | Weighted-random selection. The router's only call. |
| `CandidateSignals` | frozen dataclass | One snapshot: `base_weight, error_rate, mean_latency_s, rpm_remaining, rpm_cap, tpm_remaining, tpm_cap, breaker`. |
| `health_score(error_rate, mean_latency_s, target_latency_s) -> float` | function | `(1 - error_rate) × target / (target + observed)`. |
| `budget_score(rpm_remaining, rpm_cap, tpm_remaining, tpm_cap) -> float` | function | `min(rpm_remaining/cap, tpm_remaining/cap)`. |
| `effective_weight(base, health, budget, breaker, floor) -> float` | function | `0` if `breaker is OPEN`; else `base * health * budget`, floored to 0 below `floor`. |

### `observe.py`

| Symbol | Type | Purpose |
|---|---|---|
| `Observer` | class | Per-replica writer; pipes counts into Redis hash buckets. |
| `Observer.__init__(*, state: RedisState, window_s: int = 60, now_s_fn=time.time)` | constructor | `state` is the shared Redis facade; `window_s` matches `routing.health_window_s`. |
| `Observer.record_success(cand, *, latency_s)` | async | Pipeline: `successes+=1`, `latency_sum_ms+=...`, `latency_count+=1`, `EXPIRE window*4`. |
| `Observer.record_failure(cand, *, kind: str)` | async | Pipeline: `failures+=1`, `fail_{Kind}+=1`, `EXPIRE`. `kind` is the `ProviderError` class name. |
| `Observer.aggregate(cand) -> WindowAggregate` | async | Walks `window_s` seconds of buckets; returns sums + derived rates. |
| `WindowAggregate` | frozen dataclass | `successes, failures, total, mean_latency_s, error_rate`. |

### `refresh.py`

| Symbol | Type | Purpose |
|---|---|---|
| `RefreshTask` | class | Background task that ticks at `routing.refresh_interval_ms`. |
| `RefreshTask.__init__(*, config, observer, bucket, breakers, engine, available_providers=None)` | constructor | All collaborators injected. `available_providers` filters out missing-vendor candidates. |
| `RefreshTask.tick()` | async | One refresh cycle. Called by the lifespan once at boot, then by `_loop` repeatedly. |
| `RefreshTask.start()` | sync | Create the asyncio task. Idempotent. |
| `RefreshTask.stop()` | async | Set the stop event and `await` the task. |
| `build_signals(cfg, observer, bucket, breakers, *, available_providers=None) -> dict[CandidateRef, CandidateSignals]` | async function | The pure computation. Refresh-able from tests without an asyncio task. |

## Internals

### Weight derivation

`CandidateSignals` is the join of three independent streams: configured `base_weight`, observed health (success/failure ratio and mean latency), and current budget headroom (bucket remaining / cap). The `WeightEngine` recomputes the effective weight on every `pick()` rather than caching it, because the scores are cheap and recomputing means a new `RoutingConfig` (e.g. via SIGHUP-reload changing `target_latency_s`) takes effect on the next pick without a cache rebuild.

```python
def health_score(*, error_rate, mean_latency_s, target_latency_s) -> float:
    error_factor   = max(0.0, 1.0 - error_rate)
    latency_factor = target_latency_s / (target_latency_s + max(0.0, mean_latency_s))
    return error_factor * latency_factor
```
`weights.py:35-45`

The latency term is intentionally smooth: at `mean_latency == target`, the factor is `0.5`; at `2 × target`, it's `~0.33`; at `0`, it's `1.0`. No cliff, no piecewise behavior. This pairs with `(1 - error_rate)` which *does* have a cliff at 100% failure.

```python
def budget_score(*, rpm_remaining, rpm_cap, tpm_remaining, tpm_cap) -> float:
    if rpm_cap <= 0 or tpm_cap <= 0:
        return 0.0
    return min(max(0.0, rpm_remaining / rpm_cap), max(0.0, tpm_remaining / tpm_cap))
```
`weights.py:48-55`

The `min` is correct: a candidate is only as available as its tighter dimension. A vendor with TPM in surplus but RPM near zero must look near-zero.

```python
def effective_weight(*, base, health, budget, breaker, floor) -> float:
    if breaker is BreakerState.OPEN: return 0.0
    w = base * health * budget
    return w if w >= floor else 0.0
```
`weights.py:58-69`

`HALF_OPEN` candidates are *not* zeroed here — they retain their multiplicatively-decayed weight so a half-open probe can still receive routing if no other candidate dominates. The probe lock in `BreakerSet.try_probe` is what actually gates concurrent probes; this is consistent with the architecture overview §7.

The floor of `min_weight_floor` (default `0.02`) prevents a candidate with a ~0 product from being selected by sheer RNG luck and incurring a (likely) failed vendor call.

### `pick()` — the hot-path read

```python
def pick(self, tier_candidates, exclude, rng) -> CandidateRef | None:
    cands, weights = [], []
    for t in tier_candidates:
        ref = CandidateRef(provider=t.provider, model=t.model)
        if ref in exclude: continue
        w = self._weight(ref)
        if w <= 0.0: continue
        cands.append(ref); weights.append(w)
    if not cands: return None
    total = sum(weights)
    r = rng.random() * total
    acc = 0.0
    for c, w in zip(cands, weights, strict=True):
        acc += w
        if r <= acc: return c
    return cands[-1]
```
`weights.py:109-135`

Linear scan plus a single RNG draw; no allocations beyond the per-call lists. The final `return cands[-1]` is a defensive belt for floating-point edge cases where `r` rounds slightly above the sum.

The `_cache` lookup inside `_weight()` (`weights.py:86-89`) returns weight 0 for any candidate not in the cache. That makes the `available_providers` filtering in `RefreshTask` work transparently: an excluded provider is simply absent from the dict, and `pick()` skips it.

### Observer — per-second hash buckets

The key shape is `gw:obs:{provider}:{model}:{epoch_sec}` built by `RedisState.observe_key`. Each second-bucket is a hash with up to four fields used for aggregation plus one per error-kind counter:

```python
async def record_success(self, cand, *, latency_s):
    epoch_sec = int(self._now_s())
    key = self._state.observe_key(cand.provider, cand.model, epoch_sec)
    pipe = self._state.client.pipeline(transaction=False)
    pipe.hincrby(key, "successes", 1)
    pipe.hincrby(key, "latency_sum_ms", int(latency_s * 1000))
    pipe.hincrby(key, "latency_count", 1)
    pipe.expire(key, self._window_s * 4)
    await pipe.execute()
```
`observe.py:52-60`

`record_failure` is the same shape but writes `failures` and `fail_{Kind}` (where `Kind` is the `ProviderError` class name, e.g. `fail_RateLimited`). The router calls these at `router.py:208` (failure) and `router.py:225` (success).

The TTL is `window_s * 4`. The 4× factor is so a brief Redis hiccup during `aggregate()` (which does one HMGET per second-bucket in the window) can't race against expiry mid-pipeline. At `window_s = 60`, buckets live 240s.

The pipelines are non-transactional. Each `HINCRBY` is atomic on its own; aggregation reads can see "successes incremented but latency_sum not yet" — fine because the next refresh tick (≤ 1s later) will see the full row.

`aggregate()` (`observe.py:71-97`) reads every second-bucket from `now - window_s` to `now` inclusive (`window_s + 1` keys), pipelines `HMGET` against each, and sums in Python:

```python
total = successes + failures
mean_latency_s = (latency_sum_ms / latency_count / 1000.0) if latency_count else 0.0
error_rate     = (failures / total) if total else 0.0
```
`observe.py:88-90`

`mean_latency_s` is `0.0` when there have been no successes (no latency observations yet). `error_rate` is `0.0` when there's been no traffic — i.e. an unobserved candidate starts at "perfectly healthy". This is intentional: a brand-new candidate gets full health weight on first refresh and inherits its budget from RPM/TPM headroom.

The doc-string comment at `observe.py:13-14` is honest: this is mean, not p95. T-digest serialization would be required for p95; at 20 RPS the simplification is reasonable.

### `RefreshTask` — the background reconciler

`build_signals` is the pure function. It walks every candidate that appears in any tier (deduplicated via `_all_candidates` at `refresh.py:24-37`, first-occurrence-wins), optionally filters by `available_providers`, then does three Redis reads per candidate:

```python
agg = await observer.aggregate(cand)
rpm_remaining, tpm_remaining = await bucket.remaining(cand.provider, cand.model)
brk = await breakers.state(cand.provider, cand.model)
```
`refresh.py:64-66`

`build_signals` also calls `await breakers.refresh_snapshot()` *once at the top* (`refresh.py:56`) so that `breakers.state(...)` calls inside the loop are cheap dict reads. This is the breaker subsystem's contract — see [`breaker.md`](breaker.md).

`RefreshTask._loop` (`refresh.py:115-141`) is an `asyncio` periodic loop with **jittered exponential backoff on consecutive failures** (cr-1 §6.2 fix). On success, the base interval is restored. On failure, `consecutive_failures += 1`, `REFRESH_ERRORS_TOTAL.inc()`, and the next sleep is `min(base × 2^(failures-1), 30.0) × U(0.5, 1.0)`:

```python
# refresh.py:127-137
except Exception:
    consecutive_failures += 1
    REFRESH_ERRORS_TOTAL.inc()
    if consecutive_failures == 1:
        log.exception("refresh tick failed")
    backoff = min(
        max_backoff_s,
        base_interval_s * (2 ** (consecutive_failures - 1)),
    )
    next_wait_s = backoff * (0.5 + random.random() * 0.5)
```

A tick failure is logged once (on the first failure of a run), counted on every failure, and the loop continues at an exponentially larger interval up to a 30s cap. The 50–100% jitter window avoids synchronised retries across replicas. A transient Redis outage degrades the routing decision (the cache goes stale) but doesn't kill the task; weights eventually reflect the staleness, and on the first successful tick `consecutive_failures` is reset and `next_wait_s` returns to the configured `base_interval_s`. That's "graceful degradation" by design.

The lifespan calls `await refresh.tick()` *once synchronously* before serving (`app.py:168`), so the first request never sees an empty cache.

### `available_providers` filtering

Real-mode boot tolerates missing API keys: `build_vendors` silently skips a vendor whose key is absent. The `vendors` dict that results is passed as `available_providers=set(vendors.keys())` into `RefreshTask` (`app.py:166`). The filter at `refresh.py:59-60` then excludes those candidates from the cache. `WeightEngine._weight` returns 0 for any missing key, so `pick()` never returns a candidate the router can't actually call.

This is the *only* mechanism that prevents "vendor missing" 503s in steady state. The `vendor is None` belt at `router.py:185-188` is the runtime safety net for the brief windows where the cache hasn't caught up (e.g. a key was revoked between ticks).

## Concurrency model

- **Three asyncio collaborators, one loop.** `WeightEngine` is purely synchronous (no awaits in its methods). `Observer.record_*` and `RefreshTask.tick()` are coroutines that await Redis I/O.
- **One in-process `WeightEngine` per replica.** The `_cache` is a `dict` *replaced* (not mutated) by `update_cache`. The replacement is a single Python assignment, atomic at the interpreter level. Readers in `pick()` see either the old dict or the new one — never a torn state. `weights.py:79-81` documents this explicitly.
- **One `RefreshTask` per replica.** Owns one `asyncio.Task` (`name="routing-refresh"`) and one `asyncio.Event`. Reentrancy-safe `start()` (`refresh.py:125-127`).
- **`Observer` is shared by the router across all in-flight requests.** Concurrent `record_*` calls are independent pipelines; Redis serializes the underlying `HINCRBY`s. There is no per-instance lock.
- **No locks anywhere in this package.** Correctness rests on:
  1. `update_cache` doing dict-replacement, not dict-mutation.
  2. Redis being the source of truth for cross-replica state; in-process reads are best-effort caches.
- **`RefreshTask` ↔ `BreakerSet`.** `build_signals` calls `breakers.refresh_snapshot()` first, then individual `breakers.state()` reads. The breaker snapshot is also a rebuild-then-swap dict (`breaker.py:96-158`, #6.1), consistent with the same discipline.

## Failure modes

| Condition | Local behavior | Visible effect |
|---|---|---|
| Redis unreachable during `Observer.record_*` | Pipeline raises; propagates through `Router.route` (router does not catch). | 500 to the caller. |
| Redis unreachable during `RefreshTask.tick()` | Caught by `_loop`'s `try/except`; first failure of a run logged at error level; `REFRESH_ERRORS_TOTAL.inc()` on every failure; next sleep grows by `min(base × 2^(failures-1), 30s) × U(0.5,1.0)`. | Subsequent ticks retry with backoff. Weight cache stays at last known values. First success resets to `base_interval_s`. |
| Redis returns partial pipeline result | Each `HMGET` is independent; missing fields default to `0` via `int(row[i] or 0)`. | No-op. |
| `aggregate()` over an empty window | `total = 0`, `error_rate = 0.0`, `mean_latency_s = 0.0`. | Candidate looks "perfectly healthy", weight = `base × 1 × budget`. |
| `_weight()` called for a candidate not in `_cache` | Returns 0. | `pick()` skips it. |
| Tier config references a `(provider, model)` not in `rate_limits` | Boot-time `Config` validation rejects (`models.py:107-120`). | Lifespan crashes; uvicorn fails to start. |
| Same `(provider, model)` listed in two tiers with different `weight`s | `_all_candidates` keeps the first occurrence (`refresh.py:32-37`). | Silent config smell. Documented in the source. |
| `available_providers` excludes everything in a tier | Cache for that tier is empty; `pick()` returns `None` immediately. | 503 `UPSTREAM_UNAVAILABLE` on every request to that tier. |
| `base_weight` is 0 for a candidate | `effective_weight` returns 0 (already < floor). | Candidate is never picked. Operator-controlled disable. |
| Mid-tick config swap via SIGHUP | `RefreshTask._cfg` is captured at construction; SIGHUP-reload does not replace the field. | New `tiers` / `routing` settings are *not* picked up until restart. (Open question — see below.) |

Error taxonomy at the *attempt* level lives in `gateway/errors.py`; the per-error-kind fail counter is what the observer writes (`fail_RateLimited`, `fail_Transient5xx`, etc.). Today nothing reads those per-kind counters — they exist for future health policies (e.g. "open the breaker for rate-limited only" or "demote latency-of-the-rate-limited samples"). Reserved bandwidth, not load-bearing.

## Configuration knobs

`RoutingConfig` (`gateway/models.py:86-92`):

| Knob | Type | Default | Read by | Effect |
|---|---|---|---|---|
| `refresh_interval_ms` | `PositiveInt` | `1000` | `RefreshTask._loop` | Tick cadence. Lower = fresher weights, higher Redis load. |
| `health_window_s` | `PositiveInt` | `60` | `Observer.__init__` via `app.py:158` | Sliding-window length for `aggregate`. Affects `mean_latency_s` and `error_rate` smoothness. |
| `target_latency_s` | `float > 0` | `3.0` | `health_score` | The "healthy" latency knee. Higher = more tolerance for slow vendors. |
| `min_weight_floor` | `NonNegativeFloat` | `0.02` | `effective_weight` | Candidates below this product weight are zeroed. |
| `rng_seed_env` | `str \| None` | `None` | `app.py:171-176` | If set, names an env var whose integer value seeds the routing RNG (tests). |

Indirectly consumed:

- `Config.tiers[*]` — universe of candidates; each `TierEntry.weight` is the `base_weight`.
- `Config.rate_limits[provider/model]` — read by `build_signals` to populate `rpm_cap` / `tpm_cap` for the `budget_score`.
- `BreakerSet` defaults (`window_s=30, min_samples=20, open_duration_s=30, failure_threshold=0.30`) — not configurable from YAML in the current code.

## Open questions / known gaps

- **cr-1 §6.2 (Redis-down handling) — partly closed in commit `ea5b60c`.** `RefreshTask._loop` now backs off exponentially (capped at 30 s) with 50–100% jitter on consecutive failures and emits `REFRESH_ERRORS_TOTAL` per failed tick, so a Redis outage no longer floods logs/metrics at the base cadence. Still open: there is no in-process `REDIS_DOWN` gauge update on the failure path — see [`observability.md`](observability.md) for the open `REDIS_DOWN` gap.
- **SIGHUP doesn't reach `RefreshTask`.** `RefreshTask._cfg` is captured at construction. A `config.yaml` reload swaps `ConfigHolder.value` but `RefreshTask` keeps using the old `cfg`. To pick up tier or routing-config changes the operator must restart the replica. Cross-cutting fix: pass the holder instead of the config snapshot.
- **No per-error-kind weighting.** The Observer writes `fail_{Kind}` counters but nothing consumes them. A failure storm of `RateLimited` is treated the same as a storm of `Transient5xx` for health purposes — both reduce `error_rate`. The breaker has the same property by design; the open question is whether the *health score* should differentiate (e.g. treat `RateLimited` as a budget signal rather than a health signal).
- **`Observer.record_failure` runs for non-retryable errors too.** A caller hammering a candidate with malformed `BadRequest` payloads degrades that candidate's health for every other caller. Cheap mitigation: pass `kind` to the router and skip `record_failure` for `BadRequest`/`AuthError`/`ContentFiltered`. Today the router records uniformly (see `router.py:208` and the [`router.md`](router.md) Open questions).
- **Redis aggregation does `window_s + 1` HMGETs per tick per candidate.** At `health_window_s=60`, that's 61 pipelined commands × `len(candidates)` per tick. At 1Hz the load is fine; if the window grows or candidate count multiplies this becomes the dominant Redis cost. A Lua-side aggregation would compress to one round-trip per candidate.
- **`available_providers` is a snapshot at boot.** If an API key is added after startup (e.g. via a secrets backend that hot-rotates), the candidate stays excluded until restart. Real-mode operators should treat the key set as fixed for the lifetime of the process.
- **Mean, not p95.** `health_score` uses mean latency. One slow request per minute drags the score in proportion to the prompt-token weight, regardless of how many fast requests there were. T-digest is overkill at current scale; revisit at higher RPS.
- **`Observer.aggregate` is called from `build_signals` regardless of breaker state.** A candidate whose breaker is OPEN still costs `window_s + 1` HMGETs per tick. Cheap optimization: skip the aggregate call when the breaker is OPEN and synthesize a `WindowAggregate(0,0,0,0.0,0.0)`.
- **No metrics on tick latency.** Slow ticks (Redis tail latency) are invisible without enabling structlog-level inspection. A `gateway_refresh_tick_seconds` histogram would help operators see when the routing brain is degrading.

For the cross-replica state shape (key names, Lua scripts, atomicity guarantees) see [`redis-state.md`](redis-state.md). For the breakers `RefreshTask` reads from see [`breaker.md`](breaker.md). For the bucket `build_signals` polls see [`ratelimit.md`](ratelimit.md).
