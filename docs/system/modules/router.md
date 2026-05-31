# gateway/router.py — Adaptive failover router

## Purpose

The router is the gateway's hot path. It accepts one validated `ChatCompletionRequest` + `Caller`, picks a `(provider, model)` candidate, calls the vendor, and on retryable failure picks a different candidate and tries again — all inside one global deadline. It is also the only place the gateway translates the `ProviderError` taxonomy into either "fail over silently" or "surface a 4xx to the caller", and the only place that decides whether to consume a token from the rate-limit bucket before attempting a vendor.

It sits one level below `gateway/app.py` (which arms the metrics and pumps `AttemptRecord`s into the accounting queue) and one level above the routing subsystem (which feeds it weights — see [`routing.md`](routing.md)), the rate limiter ([`ratelimit.md`](ratelimit.md)), and the circuit breakers ([`breaker.md`](breaker.md)). Every collaborator is injected at construction; the router does no I/O of its own beyond the calls it dispatches.

## Public surface

| Symbol | Type | Purpose |
|---|---|---|
| `Router` | class | The failover loop. Single instance per replica. |
| `Router.route(req, caller) -> RouterResult` | async method | The hot path. Raises `RouterError` on failure. |
| `RouterError` | dataclass(Exception) | Carries `kind: RouterErrorKind`, `body: ErrorBody`, `tried: list[(CandidateRef, str)]`. |
| `RouterErrorKind` | str Enum | `INVALID_REQUEST`, `AUTH`, `UPSTREAM_UNAVAILABLE`, `DEADLINE_EXCEEDED`. |
| `RouterResult` | dataclass | `response: ChatCompletionResponse` plus `attempts: list[AttemptRecord]` (one per attempt, success+failure). |
| `default_clock_s() -> float` | function | The default monotonic clock; overridable in tests. |

### `Router.__init__` parameters

| Param | Type | Default | Notes |
|---|---|---|---|
| `config` | `Config` | required | Source of `tiers`, `prices`. |
| `vendors` | `dict[str, Vendor]` | required | Provider name → adapter. Missing entries cause silent candidate skip. |
| `weight_engine` | `WeightEngine` | required | Hot-path weight cache; see [`routing.md`](routing.md). |
| `bucket` | `RedisTokenBucket` | required | Atomic RPM/TPM acquire; see [`ratelimit.md`](ratelimit.md). |
| `observer` | `Observer` | required | Records per-attempt outcome to Redis; see [`routing.md`](routing.md). |
| `rng` | `random.Random` | required | `SystemRandom` in prod, seeded in tests. |
| `deadline_clock_s` | `() -> float` | `time.monotonic` | Injected for tests. |
| `total_budget_s` | `float` | `10.0` | Wall-clock deadline for the whole call. |
| `per_attempt_max_s` | `float` | `8.0` | Upper bound on a single vendor call. |
| `deadline_buffer_s` | `float` | `0.5` | Slack reserved before the deadline. |

The three timing knobs are constructor defaults. They are *not* surfaced through `Config` in the current code (see Open questions).

## Internals

### Response `id` is server-generated

`ChatCompletionResponse.id` is the gateway-generated `uuid4().hex` (`router.py:150,244`). The caller's `metadata.request_id`, if supplied, is preserved separately as `client_trace_id` and never echoed back as the response id (commit `e74a3f3`). Operators correlate via the `requests.request_id` column, the `requests.client_trace_id` column, and the per-attempt `vendor_request_id`.

### The loop shape

`Router.route` (`router.py:123-269`) is one `while True` over candidate picks. The whole structure:

```python
deadline = self._now() + self._total_budget_s
exclude: set[CandidateRef] = set()
tried:   list[tuple[CandidateRef, str]] = []
attempts: list[AttemptRecord] = []
est = estimate_tokens(prompt_chars, req.max_tokens)
request_id = uuid4().hex                                              # router.py:150
_raw_trace = (req.metadata or {}).get("request_id")
client_trace_id = str(_raw_trace)[:128] if _raw_trace is not None else None

while True:
    remaining = deadline - self._now()
    if remaining < 1.5:
        if attempts: raise RouterError(DEADLINE_EXCEEDED, ...)
        break

    cand = self._engine.pick(self._cfg.tiers[tier], exclude=exclude, rng=self._rng)
    if cand is None: break
    ...
```

The `request_id` is generated server-side once per `route()` call (commit `e74a3f3`). It is used as the response's `id` *and* as the `request_id` field on every `AttemptRecord`. The caller-supplied `metadata.request_id` is **never** used as the response id — it is truncated to 128 chars and stored separately as `client_trace_id` on every attempt row (column added by `migrations/0002_client_trace_id.sql`).

The loop has exactly five terminal arms:

1. Success → return `RouterResult` (`router.py:259`).
2. Non-retryable provider error → raise `RouterError(INVALID_REQUEST | AUTH)` (`router.py:213-221`).
3. Deadline pre-check trips with at least one attempt made → raise `RouterError(DEADLINE_EXCEEDED)` (`router.py:158-166`).
4. `pick()` returns `None` (every candidate is in `exclude` or has weight 0) → break out, raise `RouterError(UPSTREAM_UNAVAILABLE)` (`router.py:261-269`).
5. Deadline pre-check trips with no attempts made → break out, raise `UPSTREAM_UNAVAILABLE`. (Pathological — implies the clock advanced by 10s between entering `route` and the first iteration. Possible only under heavy event-loop starvation.)

The `tried` list accumulates `(CandidateRef, status)` pairs for every candidate the router actually touched (including `bucket_empty` skips and `vendor_missing` skips). It is carried inside `RouterError` for log forensics — it is not currently surfaced in the caller-visible body.

### Bucket-then-vendor ordering

The router acquires the bucket *before* checking whether the vendor adapter exists:

```python
ok, _, _ = await self._bucket.try_acquire(cand.provider, cand.model, request_tokens=est)
if not ok:
    tried.append((cand, "bucket_empty")); exclude.add(cand); continue

attempt_timeout = min(max(0.1, remaining - self._buffer_s), self._per_attempt_max_s)
t0 = self._now()
vendor = self._vendors.get(cand.provider)
if vendor is None:
    tried.append((cand, "vendor_missing")); exclude.add(cand); continue
```
`router.py:175-191`

The bucket therefore charges one RPM (and the estimated TPM) for a candidate even when the vendor turns out to be missing. This is a known minor inefficiency — at fleet scale a missing-vendor config error eats bucket headroom. The fix is to swap the two checks; it has not been done because `available_providers` in the `RefreshTask` already zeros out the weight of missing vendors so `pick()` shouldn't return them in steady state.

### Per-attempt timeout derivation

```python
attempt_timeout = min(max(0.1, remaining - self._buffer_s), self._per_attempt_max_s)
```
`router.py:183-185`

- `remaining = deadline - now`.
- The `max(0.1, ...)` floors the timeout at 100ms — never schedule a zero or negative timeout.
- The `min(..., per_attempt_max_s)` caps any single attempt at 8s by default.
- The buffer subtraction reserves time to marshal the response or build the `DEADLINE_EXCEEDED` body.

Together with the `remaining < 1.5` gate at line 156, this guarantees every attempt the router actually dispatches has at least 1.0s of vendor budget on its first try.

### `ProviderError` translation

The classes-to-status map at `router.py:79-86`:

```python
_STATUS_FOR_ERROR_KIND: dict[type[ProviderError], str] = {
    RateLimited: "rate_limited",
    Transient5xx: "transient_5xx",
    Timeout:      "timeout",
    BadRequest:   "bad_request",
    AuthError:    "auth",
    ContentFiltered: "content_filtered",
}
```

The string lands in two places: as the `status` column of the `AttemptRecord` (and hence the `requests` table), and as the `status` label on `ATTEMPTS_TOTAL`. Anything not in the dict is treated as `"transient_5xx"` (`router.py:198`) — the safe default for unrecognized provider errors.

The retryable / non-retryable split happens at the `isinstance` check on `router.py:213`:

```python
if isinstance(e, (BadRequest, AuthError, ContentFiltered)):
    http_status, body = caller_error_for(e)
    raise RouterError(
        kind=RouterErrorKind.INVALID_REQUEST if http_status == 400 else RouterErrorKind.AUTH,
        body=body, tried=tried + [(cand, status)],
    ) from e
```

`caller_error_for` (`errors.py`) returns a body whose `message` is a fixed canonical string — never the raw vendor SDK text. `vendor_detail` stays on the exception for log purposes only (#4.2). The router does *not* call `Observer.record_failure` differently for non-retryable vs retryable errors; both increment the failure counter on the same observation key. This means a flood of `BadRequest`s from a misbehaving caller will degrade that candidate's health score for *all* callers — a small but real bug. The breaker uses a separate Redis aggregation, so it's not affected.

### `AttemptRecord` building

`Router._record` at `router.py:273-320` is the single constructor for every `AttemptRecord`. Its keyword signature:

```python
def _record(self, *, request_id, caller, tier, cand, attempt_idx,
            latency_s, status,
            input_tokens=0, output_tokens=0,
            vendor_req_id=None, client_trace_id=None) -> AttemptRecord:
```
`router.py:273-287`

Every call site (`router.py:200-209` on failure, `router.py:229-241` on success) passes both the gateway-generated `request_id` and the per-request `client_trace_id`; the latter lands in the new `requests.client_trace_id` column (migration `0002_client_trace_id.sql`, commit `e74a3f3`). The cost calc prefers the vendored `PricingTable` and falls back to `cfg.prices`:

```python
if self._pricing.has(provider=cand.provider, model=cand.model):
    cost = self._pricing.cost_usd(provider=cand.provider, model=cand.model,
                                  input_tokens=input_tokens, output_tokens=output_tokens)
else:
    price = self._cfg.prices.get(cand.key())
    cost = 0.0 if price is None else (input_tokens * price.input + output_tokens * price.output) / 1_000_000
```
`router.py:288-305`

`price.input` and `price.output` are USD per 1M tokens (see `models.py:63-67`). `cand.key()` is `"{provider}/{model}"`. If a tier candidate has no price entry in either source, `Config._cross_validate_candidates_have_pricing_and_limits` at `models.py:107-120` rejects the config at boot — the `cost = 0.0` fallback is a runtime-safety belt.

`latency_ms = max(0, int(latency_s * 1000))` floors at 0 — the clock injection makes negative deltas mathematically possible under contrived test setups.

### `client_trace_id`

`router.py:151-152`:

```python
_raw_trace = (req.metadata or {}).get("request_id")
client_trace_id: str | None = str(_raw_trace)[:128] if _raw_trace is not None else None
```

The caller-provided `metadata.request_id` is copied into every `AttemptRecord` for that request, truncated to 128 chars (also enforced declaratively by `AttemptRecord.client_trace_id: max_length=128` at `models.py:258`). This is the operator's correlation handle across (gateway-generated `request_id`, vendor's `vendor_request_id`, caller's tracing id) — the caller's value never leaks into the response `id`.

## Concurrency model

- **One `Router` instance per replica.** Constructed once in `app.py` lifespan; lives until shutdown.
- **No internal state mutation.** `Router` has no mutable instance attributes (all constructor args are stored read-only). Every per-request piece of state — `deadline`, `exclude`, `tried`, `attempts`, `request_id` — is a local in `route()`.
- **Concurrent `route()` calls are independent.** Two parallel requests in flight share the same `WeightEngine`, `RedisTokenBucket`, `Observer`, and `vendors` dict, but each maintains its own `exclude` set. There is no per-router lock.
- **The shared collaborators are themselves thread-safe at the *async* level**: `WeightEngine.pick` does pure dict reads; `RedisTokenBucket.try_acquire` is an atomic Lua script; `Observer.record_*` uses non-transactional pipelines (a torn write is possible but each field is `HINCRBY`, which is itself atomic).
- **The clock function** (`self._now`) is called from a single coroutine at a time per request; no synchronization concern.
- **The RNG** is the one passed in via `app.py:181-187`. `random.SystemRandom` is thread-safe for `random()`. The seeded fallback (`random.Random(int(seed))`) is *not* concurrent-safe, which is fine because seeded mode is for tests only.

## Failure modes

| Condition | Internal handling | Caller sees |
|---|---|---|
| Unknown tier | Raised immediately, no candidate considered. | 400 `RouterError(INVALID_REQUEST, "unknown tier 'X'")` |
| `pick()` returns `None` on every iteration | Break out of loop, raise after | 503 `RouterError(UPSTREAM_UNAVAILABLE, "all candidates exhausted")` |
| Bucket empty for every candidate | Each excluded with `bucket_empty`; eventually `pick()` returns `None` | 503 `UPSTREAM_UNAVAILABLE` |
| Vendor missing for every candidate | Each excluded with `vendor_missing` | 503 `UPSTREAM_UNAVAILABLE` |
| Deadline drains with no successful attempt | Pre-check trip when `remaining < 1.5` | 504 `DEADLINE_EXCEEDED` (with `attempts` populated; included in `tried`) |
| Vendor `RateLimited` / `Transient5xx` / `Timeout` | Recorded as failure on observation; excluded; loop continues | (eventually 503 or 504 if nothing recovers; or 200 if a sibling succeeds) |
| Vendor `BadRequest` / `ContentFiltered` | Recorded as failure on observation; raised immediately | 400 with canonical message from `caller_error_for` |
| Vendor `AuthError` | Same; raised immediately | 401 with canonical message |
| Vendor raises non-`ProviderError` exception | *Not caught.* Escapes the router. | 500 (FastAPI default). This is a contract violation by the vendor adapter. |
| Redis unreachable during `try_acquire` | The `redis.RedisError` is not caught by the router. | 500 |
| Redis unreachable during `record_success` / `record_failure` | Same — propagates out of `route()`. | 500 |
| Clock jumps backward (NTP step) | `remaining` may be larger than expected; harmless. | 200 (eventually) |
| Clock jumps forward | `remaining < 1.5` trip; if `attempts` is empty, falls through to `UPSTREAM_UNAVAILABLE` instead of `DEADLINE_EXCEEDED`. | 503 (slightly wrong code but rare) |

The error taxonomy is `gateway/errors.py`. See [`../data-plane.md`](../data-plane.md) "Error paths" for the HTTP-status mapping including the `RouterError → HTTPException` translation done by `app.py:_http_status_for`.

## Configuration knobs

The router's own knobs are constructor args, not config:

| Knob | Default | Source | Effect |
|---|---|---|---|
| `total_budget_s` | `10.0` | `Router.__init__` | Whole-request deadline. |
| `per_attempt_max_s` | `8.0` | `Router.__init__` | Cap on one vendor call. |
| `deadline_buffer_s` | `0.5` | `Router.__init__` | Slack reserved before deadline. |
| `deadline_clock_s` | `time.monotonic` | `Router.__init__` | Clock; overridable for tests. |
| `rng` | `random.SystemRandom()` or seeded from `routing.rng_seed_env` | `app.py:190-195` | Routing RNG, passed to `WeightEngine.pick`. |

Knobs the router reads indirectly:

| Source | Field | Read at | Effect |
|---|---|---|---|
| `Config.tiers` | the tier list | `router.py:127, 170` | Universe of candidates. |
| `Config.prices` | per-`(provider, model)` USD/1M | `router.py:299` (fallback only — `PricingTable` is primary, `router.py:288`) | `AttemptRecord.cost_usd`. |
| `RoutingConfig.min_weight_floor` | `0.02` | `WeightEngine.pick` indirectly | Below-floor candidates are zeroed. |
| `RoutingConfig.target_latency_s` | `3.0` | `health_score` indirectly | Latency penalty curve. |
| `RoutingConfig.refresh_interval_ms` | `1000` | `RefreshTask` indirectly | How often the weight cache the router reads is refreshed. |
| `RoutingConfig.rng_seed_env` | `None` | `app.py` | If set, names the env var whose integer value seeds the RNG (tests). |

Things the router does *not* control: rate-limit caps (live in `Config.rate_limits`, consumed by `RedisTokenBucket`); breaker thresholds (live on `BreakerSet.__init__`, defaulted in `app.py`); observation window (`Observer(window_s=cfg.routing.health_window_s)`).

## Open questions / known gaps

- **Timing knobs aren't in `Config`.** `total_budget_s`, `per_attempt_max_s`, `deadline_buffer_s` are constructor defaults wired by `app.py`. Surfacing them via `RoutingConfig` would let operators tune deadlines without a redeploy.
- **Bucket charged before vendor-missing check.** A missing-vendor candidate consumes bucket headroom (`router.py:175-191`). Cheap to fix; not done yet because steady-state `available_providers` filtering in `RefreshTask` should prevent it.
- **No Redis error handling.** `RedisTokenBucket.try_acquire`, `Observer.record_success`, and `Observer.record_failure` all propagate `redis.RedisError` straight out of `route()`. See `cr-1.md` §6.2 — when Redis is down the gateway becomes a 500 factory and the `REDIS_DOWN` gauge is not flipped.
- **No per-caller fairness inside a tier.** A noisy caller can exhaust the bucket for everyone targeting the same tier. The daily token cap is the only per-caller limit.
- **Non-retryable errors still degrade health.** `Observer.record_failure` is called for `BadRequest` etc. before the router knows to give up. A caller hammering a candidate with malformed requests will lower that candidate's health score for everyone.
- **No jitter on the deadline.** All replicas see the same `total_budget_s`, so a vendor that consistently times out at exactly `8.0s` will be retried at uniform intervals across the fleet. In practice the per-attempt timeout shrinks each iteration so the second attempt is shorter, but it's still uniform across replicas.
- **`tried` is not surfaced.** It exists on `RouterError` but `_http_status_for` and the caller-visible body never expose it. Useful debug info that today lives only in the exception chain.
