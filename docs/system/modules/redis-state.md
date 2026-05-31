# `gateway/redis_state.py` — Redis coordination primitives

## Purpose

This module is the single boundary between gateway code and the `redis.asyncio` client. It owns the **three coordination primitives** the gateway depends on for fleet-wide correctness:

1. An atomic two-dimensional (RPM + TPM) token-bucket acquire — the primitive the rate limiter is built on (see [`ratelimit.md`](ratelimit.md)).
2. An opportunistic clamp that shrinks the bucket counters when a vendor's `x-ratelimit-remaining-*` headers report less than we hold locally.
3. A fleet-wide probe lock used by the circuit breaker so exactly one replica probes a half-open vendor at a time (see [`breaker.md`](breaker.md)).

Everything else in the gateway that touches Redis (`BreakerSet` increment pipelines, `Observer` sample writes, `RefreshTask` SCANs) reuses the `Redis` client exposed via `RedisState.client` rather than going through this module. Conceptually `RedisState` owns the *Lua scripts* and the *key namespace*, not all Redis I/O.

## Public surface

Importable from `gateway.redis_state`:

| Symbol | Type | Notes |
|---|---|---|
| `RATELIMIT_LUA` | `str` | Source of the two-dim token bucket script. |
| `CLAMP_LUA` | `str` | Source of the remaining-counter clamp script. |
| `LoadedScripts` | `dataclass(slots=True)` | Holds the SHA1 returned by `SCRIPT LOAD` for each script. Fields: `ratelimit: str`, `clamp: str`. |
| `RedisState` | `class` | The wrapper. Construct with a `redis.asyncio.Redis`. |
| `RedisState.client` | `property -> Redis` | Escape hatch so callers (`BreakerSet`, `Observer`) can issue arbitrary commands on the same connection pool. |
| `RedisState.load_scripts()` | `async -> LoadedScripts` | Idempotent — loads both scripts and memoises the result. |
| `RedisState.bucket_key(provider, model)` | `-> str` | |
| `RedisState.breaker_key(provider, model)` | `-> str` | |
| `RedisState.breaker_probe_key(provider, model)` | `-> str` | |
| `RedisState.observe_key(provider, model, epoch_sec)` | `-> str` | |
| `RedisState.ratelimit_acquire(bucket_key, *, now_ms, rpm_cap, tpm_cap, refill_per_ms_rpm, refill_per_ms_tpm, request_tokens)` | `async -> tuple[bool, int, int]` | `(accepted, rpm_remaining, tpm_remaining)`. Both counts are post-state and post-refill, even when `accepted == False`. |
| `RedisState.ratelimit_clamp(bucket_key, *, rpm_observed, tpm_observed)` | `async -> tuple[int, int]` | `(rpm_after, tpm_after)`. |
| `RedisState.acquire_probe_lock(probe_key, *, holder, ttl_s)` | `async -> bool` | Plain `SET NX EX`. |

## Key layout

All keys live under the `gw:` namespace. Format strings are class attributes on `RedisState` (`redis_state.py:93-97`).

| Constant | Pattern | Type | TTL | Owner |
|---|---|---|---|---|
| `KEY_BUCKET` | `gw:bkt:{provider}:{model}` | hash (`rpm_remaining`, `tpm_remaining`, `last_refill_ms`) | 600 000 ms (refreshed every acquire) | `RedisTokenBucket` |
| `KEY_BREAKER` | `gw:brk:{provider}:{model}` | (reserved — not written by the current implementation) | — | `BreakerSet` |
| `KEY_BREAKER_PROBE` | `gw:brk:{provider}:{model}:probe` | string | 10 s (via `SET EX`) | `BreakerSet.try_probe` |
| `KEY_OBSERVE_SEC` | `gw:obs:{provider}:{model}:{epoch_sec}` | hash | written by `Observer`, TTL set there | `Observer` |
| `CHANNEL_BREAKER` | `gw:brk-events` | pub/sub channel | — | **declared but unused** |

Two sample-key families are owned outside this module but coexist in the namespace:

- `gw:brk:{provider}:{model}:samples:{epoch_sec}` — breaker per-second sample hashes, written by `BreakerSet._increment` (see [`breaker.md`](breaker.md)).
- `gw:obs:{provider}:{model}:{epoch_sec}` — observer per-second aggregation hashes.

> **Gap.** `CHANNEL_BREAKER` is reserved for breaker state-transition pub/sub but no publisher or subscriber exists today. The breaker module docstring still talks about it as a future addition; commit `8e046e5` (cr-1 §6.1 atomic snapshot rebuild) did **not** add the pub/sub fast-path. See cr-1 §6 and t-1 §4 missing scenarios.

## Internals

### `LoadedScripts` and the `EVALSHA → EVAL` fallback

`load_scripts()` calls `SCRIPT LOAD` once and caches the returned SHA1s in a `LoadedScripts` slots dataclass:

```python
# redis_state.py:107-113
async def load_scripts(self) -> LoadedScripts:
    if self._scripts is None:
        self._scripts = LoadedScripts(
            ratelimit=await self._r.script_load(RATELIMIT_LUA),
            clamp=await self._r.script_load(CLAMP_LUA),
        )
    return self._scripts
```

`_eval()` runs every script via `EVALSHA` and transparently falls back to `EVAL` on `NoScriptError`:

```python
# redis_state.py:115-126
async def _eval(self, body, sha, numkeys, *args):
    try:
        return await self._r.evalsha(sha, numkeys, *args)
    except NoScriptError:
        return await self._r.eval(body, numkeys, *args)
```

This matters because (a) real Redis evicts the script cache under memory pressure, (b) a freshly-started replica or a failed-over node has an empty cache, and (c) `fakeredis` in tests has its own quirks around script storage. The fallback is the standard idiomatic handling and adds zero overhead on the hit path.

### Lua script #1 — `RATELIMIT_LUA`

Atomic two-dim token-bucket acquire with lazy refill. Source at `redis_state.py:27-63`.

**Contract** — inputs (one key, six args):

| Slot | Meaning |
|---|---|
| `KEYS[1]` | bucket hash key |
| `ARGV[1]` | `now_ms` (int) — clock injected by caller |
| `ARGV[2]` | `rpm_cap` (int) |
| `ARGV[3]` | `tpm_cap` (int) |
| `ARGV[4]` | `refill_rpm_pm` (float, per ms) |
| `ARGV[5]` | `refill_tpm_pm` (float, per ms) |
| `ARGV[6]` | `req_tokens` (int) |

**Output** — `[ok, rpm_remaining, tpm_remaining]`, where `ok ∈ {0,1}` and the two counts are integers (`math.floor` is applied — see "Why integers" below).

**Algorithm** — read the three hash fields; default any nil field (first touch) to cap / `now_ms`; advance by `elapsed * refill_per_ms` clamped at the cap; if both dimensions have enough headroom, decrement `1` from RPM and `req_tokens` from TPM and set `ok = 1`; write the post-state back with `HMSET` and `PEXPIRE 600 000 ms`.

**Why atomic.** All reads, the refill arithmetic, the conditional, the writes, and the TTL refresh happen inside one `EVAL`/`EVALSHA`. Redis runs Lua single-threaded, so no other client can interleave between the `HMGET` and the `HMSET` — partial deductions are impossible.

**Why `request_tokens=0` is the "remaining" read.** A zero-cost acquire always satisfies `tpm_rem >= 0`, and `rpm_rem >= 1` if any quota remains, so the script consumes 1 RPM and returns the refilled post-state — that's the contract `RedisTokenBucket.remaining()` depends on.

**Falls back to `EVAL` when** the loaded SHA is missing from the server-side cache. This is invisible to callers.

### Lua script #2 — `CLAMP_LUA`

Opportunistic shrink-only update. Source at `redis_state.py:67-81`.

**Contract** — one key, two args:

| Slot | Meaning |
|---|---|
| `KEYS[1]` | bucket hash key |
| `ARGV[1]` | `rpm_observed` (int, from vendor headers) |
| `ARGV[2]` | `tpm_observed` (int) |

**Output** — `[rpm_after, tpm_after]` (integers via `math.floor`).

**Algorithm** — read current `rpm_remaining` and `tpm_remaining`; if the field is missing (no prior acquire on this bucket), seed from the observed value; for each dimension, take `min(current, observed)`; `HSET` back.

**Why atomic.** Same reason as `RATELIMIT_LUA` — `HMGET` + comparisons + `HSET` is one Lua call.

**Why shrink-only.** A vendor's `x-ratelimit-remaining` can lie low (cached, stale, or sliding-window edge effects) but rarely lies high. Shrinking is safe — at worst we get short-term over-throttling that the next refill tick repairs. Growing on a vendor header would let a stale reading inflate the bucket and cause real overage, so it's never done.

**Falls back to `EVAL` when** the SHA is missing — same path as above.

### Lua script #3 — probe lock (no Lua; plain `SET NX EX`)

```python
# redis_state.py:186-191
async def acquire_probe_lock(self, probe_key, *, holder, ttl_s):
    """`SET NX EX` is already atomic in Redis — no Lua needed."""
    result = await self._r.set(probe_key, holder, nx=True, ex=ttl_s)
    return bool(result)
```

`SET key value NX EX ttl` is an atomic Redis primitive since 2.6.12 — `NX` (set only if not exists) and `EX` (TTL in seconds) compose in one round trip. There is no point reimplementing it as Lua: the existing command already gives test-and-set with auto-expiry.

The `holder` arg is a small debug string (`f"probe:{int(now_s)}"` in the breaker — see `breaker.py:308`). It is **not used for ownership verification** today — there is no release/CAS path that re-reads it. If the holder dies before resolving the probe, the TTL (`10 s` by default) expires the lock and the next refresh will let another replica probe.

### Why all returned counts are integers

Redis Lua cannot reliably return floats across server builds (the protocol forces integer encoding for numeric replies), so both scripts apply `math.floor` before returning:

```lua
-- redis_state.py:62
return {ok, math.floor(rpm_rem), math.floor(tpm_rem)}
```

This is the reason `RedisState.ratelimit_acquire` typeshape is `tuple[bool, int, int]` and not `tuple[bool, float, float]`. Callers that compare `remaining` to caps do not lose precision — caps are themselves integers — but they do lose sub-token refill state, which is recovered on the next acquire from `last_refill_ms`.

## Concurrency model

- **Atomic** (Redis serializes the script): `RATELIMIT_LUA`, `CLAMP_LUA`. Each script is a single logical operation across `HMGET`/`HMSET`/`PEXPIRE`. No other connection can interleave.
- **Atomic** (Redis primitive): the probe lock via `SET NX EX`.
- **NOT atomic** (composed Redis commands): everything the `BreakerSet` does — it issues `HINCRBY` + `EXPIRE` as a non-transactional pipeline (`breaker.py:85-89`). That's fine for a counter that's only ever read in aggregate.
- **NOT atomic** across script boundaries: a `ratelimit_acquire` followed by a `ratelimit_clamp` is two scripts. A concurrent acquire on a third connection can land between them. Acceptable — clamp is best-effort.

### Probe-lock guarantee

`SET NX EX` gives **at-most-one probe in flight per `(provider, model)` for `ttl_s` seconds**. Combined with the breaker's "one HALF_OPEN → CLOSED|OPEN transition per probe result" rule, this gives the fleet effectively-once probing per outage window. The 10s TTL bounds the consequences of a holder crash: at most 10s of probe blackout, after which any replica can re-acquire.

### `fakeredis` vs real Redis

The test suite uses `fakeredis.aioredis` (see t-1 §4.5, §8.5). It serializes all commands on the asyncio event loop, which means **every Lua script and every command is implicitly atomic with respect to other coroutines in the same Python process**. This covers:

- The atomicity of the two Lua scripts.
- The atomicity of `SET NX EX`.
- The non-interleaving of the breaker's `HINCRBY`/`EXPIRE` pipeline.

What `fakeredis` does **not** cover:

- True multi-process atomicity. Real Redis serializes across connections; `fakeredis` only across coroutines in one process.
- Script-cache eviction behavior under memory pressure — the `NOSCRIPT` fallback exists but the test suite has no direct regression for it.
- `SCRIPT FLUSH` / replica failover scenarios.
- Real network errors (`ConnectionError`, `TimeoutError`).

End-to-end correctness under multi-replica load needs an integration test against real Redis. None exists today (t-1 §8.5).

## Failure modes

| Failure | Behavior |
|---|---|
| Redis unreachable (`ConnectionError`, `TimeoutError`) | Every `RedisState` method awaits a `redis.asyncio` coroutine that raises. The exception propagates through `RedisTokenBucket`, `BreakerSet`, and `Observer` up into `Router.route`, which does not catch it — caller sees HTTP 500. The `REDIS_DOWN` gauge is set at boot but never updated on runtime failure (cr-1 §6.2). The `RefreshTask` background loop, however, catches its own failures, increments `REFRESH_ERRORS_TOTAL`, and backs off exponentially — see [`routing.md`](routing.md) and [`observability.md`](observability.md). |
| `NoScriptError` (script SHA missing on server) | Transparently retried as `EVAL` via `_eval()` (`redis_state.py:123-126`). One extra round trip; no caller impact. |
| Clock skew — `now_ms` goes backwards | The Lua script handles it explicitly: `if elapsed < 0 then elapsed = 0 end` (`redis_state.py:46`). The acquire still proceeds with zero refill. Repeated backward clock readings degrade throughput (no refill) but never produce overage. The clock is injected via `RedisTokenBucket.now_ms_fn`, so in tests it can be frozen safely. |
| Bucket key TTL expires while not in use | The 600s `PEXPIRE` is refreshed on every acquire, so a bucket only expires after 10 min of zero traffic. On the next acquire, the script reseeds `rpm_rem = rpm_cap`, `tpm_rem = tpm_cap`, `last_ms = now_ms` — effectively a fresh full bucket. This is the desired behavior. |
| Partial deductions on rejection | Impossible. The decrement is inside the `if rpm_rem >= 1 and tpm_rem >= req_tokens` branch (`redis_state.py:51-56`). On rejection the script still writes back the refilled state and TTL, but never decrements. |
| Probe-lock holder dies | `SET EX` TTL (10 s default) auto-releases. The breaker observes `state == HALF_OPEN` on the next refresh and any replica can attempt `try_probe` again. |
| Clamp races acquire | A clamp arriving after an acquire writes `min(observed, current_post_acquire)`. The bucket can only shrink — never grow above cap. Worst case the next request sees a slightly tighter bucket than it would have. |

## Configuration knobs

There are no environment variables read by this module. All tunables are constructor args or live in the scripts themselves:

| Knob | Source | Default | Effect |
|---|---|---|---|
| Redis client | `RedisState(redis=...)` constructor | — | Whatever the app builds (real Redis or fakeredis in tests). |
| Bucket TTL | hard-coded in `RATELIMIT_LUA` | `600 000` ms | How long an idle bucket survives before reset. |
| Probe-lock TTL | passed by `BreakerSet.try_probe` | `10` s | How long a half-open probe can block other replicas. |
| `rpm`, `tpm` caps | `RateLimitEntry` per `(provider, model)` in `Config.rate_limits` | required | Hard fleet-wide cap. |
| `refill_per_ms_*` | derived in `RedisTokenBucket` as `rpm / 60_000`, `tpm / 60_000` | — | See [`ratelimit.md`](ratelimit.md). |
| `now_ms` | injected via `RedisTokenBucket.now_ms_fn` | `default_now_ms()` (= `int(time.time()*1000)`) | Test seam; pass a frozen function for determinism. |

## Open questions / known gaps

- **`CHANNEL_BREAKER` is dead code.** Declared at `redis_state.py:97` and referenced in the breaker module docstring, but no `PUBLISH` and no `SUBSCRIBE` exist anywhere in the codebase. Commit `8e046e5` reworked `BreakerSet.refresh_snapshot` for cr-1 §6.1 (atomic rebuild) without adding the pub/sub path. Either implement the breaker pub/sub fast-path described in `breaker.py:18-21`, or delete the constant. See t-1 §4 missing scenarios.
- **`KEY_BREAKER` is unused.** The actual breaker state lives only in `BreakerSet._snapshot` (in-process) and in the per-second sample hashes — there is no Redis-side "current state" key. The constant exists for future use (e.g. for the pub/sub fast-path, or for cross-replica state caching) but is not written today.
- **No release / CAS for the probe lock.** A probe holder cannot release early; everyone waits for the 10s TTL. Tolerable but means a fast-probe path can't recover quota on a fast success.
- **No `REDIS_DOWN` heartbeat.** The metric is set once at boot and never updated. Operators can't tell from `/metrics` whether Redis is reachable right now. (cr-1 §6.2.)
- **No integration test against real Redis.** All concurrency claims are validated only against `fakeredis`'s single-process serialization. (t-1 §8.5.)
- **No test for `NoScriptError` fallback.** The path is exercised only by hand. (t-1 §4.)
- **No clock-injection test for backward `now_ms`.** The Lua guard exists but isn't covered. (t-1 §4.)

## How the pieces compose

### Acquire path (single request, single attempt)

```
Router.route
   → RedisTokenBucket.try_acquire(provider, model, request_tokens=est)
        → RedisState.ratelimit_acquire(bucket_key, now_ms, caps, refill_rates, request_tokens)
             → RedisState._eval(RATELIMIT_LUA, sha, 1, key, *args)
                  → redis.evalsha → (NoScriptError) → redis.eval
                                  → [ok, rpm_remaining, tpm_remaining]
        ← (bool, int, int)
   ← decision: proceed with vendor.chat OR exclude candidate and repick
```

Two integers leave the Lua script as a list; the Python wrapper unpacks `(bool(int(raw[0])), int(raw[1]), int(raw[2]))` and returns a typed tuple. The cast through `int()` is defensive — `fakeredis` and real Redis both return Python ints already, but the `bytes`-vs-`int` dance has bitten enough Redis users that the explicit casts are worth keeping.

### Probe path (breaker HALF_OPEN gate)

```
BreakerSet.try_probe(provider, model)
   → RedisState.acquire_probe_lock(probe_key, holder=f"probe:{now_s}", ttl_s=10)
        → redis.set(probe_key, holder, nx=True, ex=10)
   ← bool
```

One Redis round trip, no Lua. The `holder` value is currently unused for ownership verification — it's a debug breadcrumb.

### Clamp path (vendor-header reconciliation)

```
(future) Vendor adapter post-response
   → RedisTokenBucket.clamp(provider, model, rpm_observed, tpm_observed)
        → RedisState.ratelimit_clamp(bucket_key, rpm_observed, tpm_observed)
             → RedisState._eval(CLAMP_LUA, sha, 1, key, *args)
   ← (int, int)
```

Best-effort; never grows the bucket; safe to drop on Redis failure (the next acquire still applies the cap).

## Why three primitives and not one

It would be tempting to fold the probe lock and the clamp into the acquire script, or to do per-request observability writes from inside the Lua. The split is deliberate:

- **`RATELIMIT_LUA` is on the hot path.** It runs once per attempt. Keeping it short (~25 lines of Lua) keeps it fast (~0.2 ms server-side at typical loads).
- **`CLAMP_LUA` is off the hot path.** It runs once per response (not per attempt), only when vendor headers are present.
- **`probe_lock` is a singleton operation.** It runs once per HALF_OPEN transition per `(provider, model)`. A 10s TTL gives crash-safety without ownership-tracking complexity.

Folding any of them into another script would add work to the hot path with no atomicity gain.

## Testing notes

The test suite (`tests/test_redis_state.py` — owned by another agent) covers:

- Idempotent `load_scripts` and the `_scripts` memo.
- A full acquire cycle including refill across a 1s gap.
- Rejection when `tpm_cap` is exceeded.
- Probe-lock NX semantics — second acquire returns `False` while the first holds.

Not yet covered (t-1 §4, §8):

- The `NoScriptError` fallback path.
- Backward-`now_ms` clock skew.
- Bucket TTL expiry after 10 min of idleness.
- Concurrent acquires from real (not faked) connections.
