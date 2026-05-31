# `gateway/ratelimit.py` — Two-dimensional token bucket

## Purpose

A thin, async, Redis-backed token bucket per `(provider, model)` enforcing two limits in lockstep — requests-per-minute (RPM) and tokens-per-minute (TPM). One acquire decrements both atomically; one rejection rejects both.

Why two dimensions? Vendor quotas are advertised that way (OpenAI, Anthropic, Google all expose `rpm` *and* `tpm`), and the gateway's job is to never exceed either across the fleet. Why fleet-wide via Redis? Because per-replica quota is wrong by definition — `N` replicas each enforcing `rpm/N` either over-throttles at low load or overshoots when load is uneven. The bucket is the source of truth, lazily refilled inside one Lua script (see [`redis-state.md`](redis-state.md)).

This module is a facade. It does no arithmetic of its own — every refill/decrement happens server-side in `RATELIMIT_LUA`. The Python class exists to (a) cache the `RateLimitEntry` lookup, (b) derive per-ms refill rates from per-minute caps, and (c) inject a test clock.

## Public surface

Importable from `gateway.ratelimit`:

| Symbol | Type | Notes |
|---|---|---|
| `RedisTokenBucket` | `class` | The facade. |
| `RedisTokenBucket(state, limits, now_ms_fn=default_now_ms)` | constructor | `state: RedisState`, `limits: dict[str, RateLimitEntry]` keyed `"provider/model"`. |
| `RedisTokenBucket.try_acquire(provider, model, *, request_tokens)` | `async -> tuple[bool, int, int]` | `(accepted, rpm_remaining, tpm_remaining)`. |
| `RedisTokenBucket.clamp(provider, model, *, rpm_observed, tpm_observed)` | `async -> tuple[int, int]` | `(rpm_after, tpm_after)`. |
| `RedisTokenBucket.remaining(provider, model)` | `async -> tuple[int, int]` | **Side effect:** consumes 1 RPM. See below. |
| `estimate_tokens(prompt_chars, max_tokens)` | `(int, int) -> int` | `max(1, prompt_chars // 4 + max_tokens)`. |
| `default_now_ms()` | `() -> int` | `int(time.time() * 1000)`. |

`RedisTokenBucket` raises `KeyError(f"no rate_limits entry for {provider!r}/{model!r}")` if a `(provider, model)` is asked about with no entry in the constructor map (`ratelimit.py:50-53`). This is a config error — the router treats it as a 500.

## Internals

### Construction

`__init__` stores three things: the `RedisState`, the immutable `limits` map, and a clock function (defaulting to wall time in ms). No Redis I/O at construction time — scripts load lazily on first `try_acquire`.

### `try_acquire` — the hot-path call

```python
# ratelimit.py:55-68
async def try_acquire(self, provider, model, *, request_tokens):
    entry = self._entry(provider, model)
    key = self._state.bucket_key(provider, model)
    return await self._state.ratelimit_acquire(
        key,
        now_ms=self._now_ms(),
        rpm_cap=entry.rpm,
        tpm_cap=entry.tpm,
        refill_per_ms_rpm=entry.rpm / 60_000,
        refill_per_ms_tpm=entry.tpm / 60_000,
        request_tokens=request_tokens,
    )
```

The work is one `EVALSHA` against `RATELIMIT_LUA` — see [`redis-state.md`](redis-state.md#lua-script-1--ratelimit_lua) for the script's contract. The return tuple is `(accepted, rpm_remaining, tpm_remaining)`; on rejection both remaining counts still reflect the post-refill state.

**Hot-path consumer** — `Router.route` calls this exactly once per attempt, before the vendor call (`router.py:172-178`). On rejection the candidate is added to the exclude set and the loop picks again; on acceptance the router proceeds to `vendor.chat(...)`. See [`router.md`](router.md) for the full failover loop.

The decision to acquire **before** dispatching the vendor call is deliberate: an over-the-quota request is cheap to reject locally and costly to send. The bucket guards both directions — the caller is protected from the vendor's 429s, and the vendor is protected from the fleet's overage.

#### What `request_tokens` represents

The value passed in by the router is the **estimate**, not the actual completion size — actual completion size isn't knowable until the vendor responds. This is a deliberate design choice that matches the way vendors meter at the request time, not at completion time: the cap consumed by a request equals the requested `max_tokens` plus the prompt length, regardless of the eventual finish length. Any difference between estimate and actual is small relative to typical caps and is partially corrected by `clamp`-from-headers.

#### Lookup cost

`_entry(provider, model)` does one Python dict lookup keyed `f"{provider}/{model}"`. The map is sized by the number of `(provider, model)` pairs configured in `Config.rate_limits` — typically <20 entries. No hashing or string interpolation overhead matters at this scale.

### Refill-rate derivation

Per-minute caps come from `RateLimitEntry`; the Lua script expects per-millisecond refill rates. The conversion is `cap / 60_000`:

| YAML field | Per-minute | Per-ms (passed to Lua) |
|---|---|---|
| `rate_limits.<provider>/<model>.rpm` | `entry.rpm` | `entry.rpm / 60_000` |
| `rate_limits.<provider>/<model>.tpm` | `entry.tpm` | `entry.tpm / 60_000` |

Floats are sent over the wire. Lua does the multiplication (`elapsed_ms * refill_per_ms`) at server-side double precision; the result is floored to int before being returned. The lost sub-token fractional state is recovered on the next acquire because `last_refill_ms` is stored alongside the counters — refill is computed from the elapsed `Δms` since the last write, not accumulated locally.

### `clamp` — vendor-header reconciliation

```python
# ratelimit.py:70-77
async def clamp(self, provider, model, *, rpm_observed, tpm_observed):
    self._entry(provider, model)  # validate exists
    key = self._state.bucket_key(provider, model)
    return await self._state.ratelimit_clamp(
        key, rpm_observed=rpm_observed, tpm_observed=tpm_observed
    )
```

Shrink-only. Called when a vendor response carries `x-ratelimit-remaining-requests` / `-tokens` and the value is lower than our local count. See [`redis-state.md`](redis-state.md#lua-script-2--clamp_lua) for why it never grows the bucket.

Note: as of `HEAD` the vendor adapters do not yet wire clamp calls in on every response; that integration is tracked but the primitive is ready.

### `remaining` — the documented-side-effect read

```python
# ratelimit.py:79-100
async def remaining(self, provider, model):
    """Read current (rpm_remaining, tpm_remaining) without consuming.

    Acquiring 0 tokens lazily refills and returns post-state. We use a tiny
    epsilon trick: ask for 0 RPM/TPM, which always succeeds and reveals the
    current count.
    """
    ...
    _, rpm, tpm = await self._state.ratelimit_acquire(
        key, ..., request_tokens=0,
    )
    # We did consume 1 RPM with that call. ...
    return rpm, tpm
```

`remaining()` is implemented as `try_acquire(request_tokens=0)`. The Lua script's accept condition is `rpm_rem >= 1 and tpm_rem >= req_tokens` — with `req_tokens = 0`, the TPM check is always satisfied, so the script decrements **1 RPM** and returns the (now slightly smaller) post-state.

**Consequence (documented at `ratelimit.py:96-100`):** every `remaining()` call burns one RPM token. The intended caller is the `RefreshTask` (`routing/refresh.py:65`) running at ~1 Hz — a 60 RPM tax against each candidate's `rpm_cap`. For a `rpm_cap` of 1000+ this is in the noise; for a tiny mock candidate (e.g. `rpm = 10`) it would dominate. Callers MUST NOT use `remaining()` for rate-limit correctness decisions — it is a metrics/observability helper only.

### `estimate_tokens` — TPM-acquire sizing

```python
# ratelimit.py:103-109
def estimate_tokens(prompt_chars: int, max_tokens: int) -> int:
    return max(1, prompt_chars // 4 + max_tokens)
```

Used once per request, in `Router.route` (`router.py:145-146`):

```python
prompt_chars = sum(len(m.content) for m in req.messages)
est = estimate_tokens(prompt_chars, req.max_tokens)
```

The formula is a deliberate caricature: "~4 characters per token" for the prompt, plus the caller's requested `max_tokens` ceiling for the response.

**Why ~4 chars/token is a *consistent* (not *accurate*) estimator and why that's fine.** Real tokenizers (BPE, SentencePiece) vary per-vendor and per-language; "4 chars/token" misses by ±30% routinely. But the bucket's job is to never overshoot the vendor's fleet-wide cap, and:

- The estimator is **monotonic** in input size — bigger prompt always asks for more TPM.
- It is **biased high** for English (where real tokens average ~4.5 chars), which means we err on the side of throttling sooner, not later.
- It uses the caller-supplied `max_tokens` as an upper bound for the response, which is exactly the vendor's accounting model (TPM is debited against the *cap*, not the actual completion length, at request time).
- Any drift between estimate and actual usage is absorbed by the next request's `clamp` against vendor headers — the bucket self-corrects.

What we explicitly do not need: a real tokenizer. Importing `tiktoken` or vendor SDKs in the hot path would cost 1-5 ms per request to win back maybe 5% of cap headroom. Not worth it.

### Key construction

The bucket key is `gw:bkt:{provider}:{model}` via `RedisState.bucket_key()`. There is no caller dimension and no time dimension — the same `(provider, model)` pair shares one bucket across all tiers and all callers, which is the correct semantic (vendor quotas are fleet-wide).

Cross-tier sharing matters: if the `fast` and `smart` tiers both list `openai/gpt-4o-mini` as a candidate, both contend for the same bucket. This is intentional — the vendor's quota does not care which logical tier we routed through.

### Module-level helper: `_candidate_key`

```python
# ratelimit.py:21-22
def _candidate_key(provider: str, model: str) -> str:
    return f"{provider}/{model}"
```

The same `"provider/model"` shape is used as the lookup key into `Config.rate_limits` (loaded from YAML), as the routing-engine internal key, and inside `Observer`. Keeping the format string in one helper avoids the typical "where does the slash go" drift across modules.

## Concurrency model

- **Atomic**: every `try_acquire`, every `clamp`. The Lua script is a single Redis operation. Concurrent acquires from different replicas, different coroutines, or both, are linearizable.
- **Atomic**: every `remaining()` (same script).
- **Not atomic across calls**: an acquire followed by a clamp can interleave with another replica's acquire. Acceptable — clamp is a best-effort downward correction.
- **No in-process state to share.** `RedisTokenBucket` has no mutable instance state beyond the injected `now_ms_fn`. Multiple coroutines can call `try_acquire` on the same instance concurrently with no risk.
- **`fakeredis` covers** the single-process atomicity of the Lua script (commands serialize on the event loop — see t-1 §4.5, §8.5). **Real Redis** is required to validate atomicity across replicas; no integration test exists today.

## Failure modes

| Failure | Behavior |
|---|---|
| Bucket key has no entry in `Config.rate_limits` | `KeyError` from `_entry()`. Propagates up; the router does not catch it. Caller sees 500. This is a config-validation bug — the YAML schema in `models.py` should reject unknown candidates. |
| Redis unreachable | `try_acquire` raises whatever `redis.asyncio` raises. Router does not catch it. See [`redis-state.md`](redis-state.md#failure-modes). |
| Lua script missing from cache (`NoScriptError`) | Transparently retried as `EVAL` by `RedisState._eval`. One extra round trip. |
| Clock skew (`now_ms_fn` returns a value smaller than `last_refill_ms`) | Lua guards with `if elapsed < 0 then elapsed = 0 end`. No refill that tick. Repeated skew degrades throughput but never causes overage. |
| `request_tokens == 0` (used by `remaining()`) | Accepted iff `rpm_rem >= 1`. Consumes 1 RPM. By design. |
| `request_tokens` larger than `tpm_cap` | The acquire can never succeed — `tpm_rem >= req_tokens` fails forever. The router treats this as bucket exhaustion and excludes the candidate. No special handling. Would manifest only on misconfigured caps. |
| Partial deduction on rejection | Impossible. The decrement is gated inside the accept branch (see [`redis-state.md`](redis-state.md#lua-script-1--ratelimit_lua)). |
| Bucket TTL expires (10 min idle) | Next acquire reseeds full cap. Correct. |

## Configuration knobs

| Knob | Source | Default | Effect |
|---|---|---|---|
| `state` | constructor | required | The `RedisState` to talk to. |
| `limits` | constructor | required | `dict[str, RateLimitEntry]` keyed `"provider/model"`. Loaded from `Config.rate_limits`. |
| `now_ms_fn` | constructor | `default_now_ms` (= `int(time.time()*1000)`) | Test seam. |
| `RateLimitEntry.rpm` | `Config.rate_limits.<key>.rpm` (YAML) | `PositiveInt`, required | Fleet-wide requests-per-minute cap. |
| `RateLimitEntry.tpm` | `Config.rate_limits.<key>.tpm` (YAML) | `PositiveInt`, required | Fleet-wide tokens-per-minute cap. |

The module reads no environment variables of its own and has no class-level defaults beyond `default_now_ms`.

## Open questions / known gaps

- **`remaining()` consumes 1 RPM.** Documented but easy to misuse. A separate read-only Lua script (`HMGET` + lazy refill but no decrement) would close this. The refresh tick currently pays ~60 RPM per candidate per minute on this — fine at production caps, noticeable on small test buckets. See cr-1 §6 for the broader discussion.
- **Clamp is not yet wired from vendor headers.** The primitive exists; the vendor adapters in `gateway/providers/` do not call it. Tracked separately.
- **`estimate_tokens` does not see the system prompt's tier-level overhead** (if any per-vendor preamble exists). Acceptable today; revisit if the gap from estimate to actual usage exceeds ~20% in production observation.
- **No test for `clamp` racing acquire** under real Redis (t-1 §8.5).
- **`KeyError` on unknown candidate is a runtime 500.** Belongs in config validation. Tracked in cr-1.
- **No vendor-specific tokenizer fallback.** `estimate_tokens` is the same `chars // 4 + max_tokens` formula for every vendor. If a future vendor's tokenizer diverges sharply (e.g. CJK-heavy traffic where token-per-char ratios are very different), the per-vendor under-estimate could cause sustained TPM overage. Mitigation today is the `clamp` primitive plus the breaker — TPM overage will show as `rate_limited` from the vendor, which records a failure sample.
- **No hot-path metric for bucket rejections.** A `try_acquire` rejection causes the router to add the candidate to `tried` with status `"bucket_empty"` (`router.py:175-176`), which feeds the attempt audit log, but there is no dedicated Prometheus counter for "bucket-empty rejections per `(provider, model)`". Useful for an operator dashboard; tracked.

## Call graph

```
Router.route                    ─→ try_acquire (per attempt)              ─→ RedisState.ratelimit_acquire (Lua)
Router.route                    ─→ estimate_tokens (once per request)
RefreshTask.tick / build_signals ─→ remaining (per candidate per tick)    ─→ RedisState.ratelimit_acquire (Lua, req_tokens=0)
(future) Vendor adapters        ─→ clamp (per response with headers)     ─→ RedisState.ratelimit_clamp (Lua)
```

All four arrows are single Redis round trips. There is no batching across candidates today — each candidate's bucket lives in its own key. At <100 candidates and <100 RPS this is comfortably under the latency budget.

## Worked example: estimate vs actual

Say a caller sends a 1200-character prompt and `max_tokens=512`. The router computes:

```
prompt_chars = 1200
est = max(1, 1200 // 4 + 512) = max(1, 300 + 512) = 812
```

The router calls `try_acquire(provider, model, request_tokens=812)`. The Lua script needs the bucket to hold `tpm_remaining >= 812` and `rpm_remaining >= 1`. Assume both — both counters decrement, the script returns `(True, rpm_remaining-1, tpm_remaining-812)`.

Vendor responds with actual `usage.prompt_tokens=287` and `usage.completion_tokens=391`. Real consumption: 678 tokens. The bucket held back 812. The 134-token over-charge is recovered passively — the next minute's refill replenishes the cap normally, and the over-charge is bounded by `max_tokens` (which the caller chooses).

If vendor headers reported `x-ratelimit-remaining-tokens=15000` and our local counter shows 17000, a `clamp` call shrinks the local counter to 15000. The next acquire sees the tighter cap.

## Testing notes

`tests/test_ratelimit.py` (owned by another agent) covers:

- Acquire success when bucket has headroom; rejection when TPM is exceeded.
- Lazy refill across an injected-clock gap.
- `estimate_tokens` boundary at zero (`max(1, ...)`).
- `clamp` shrinks but never grows.
- `remaining` returns post-state and burns 1 RPM (regression for the documented side effect).
- `KeyError` on a `(provider, model)` not in the `limits` map.

Not yet covered (t-1 §4, §8):

- Concurrent acquires from real Redis (only `fakeredis` today).
- Vendor adapters wiring `clamp` after responses.
- `try_acquire(request_tokens=tpm_cap+1)` — a request larger than the cap.

## Configuration example

A representative slice of `config.yaml` driving this module:

```yaml
rate_limits:
  openai/gpt-4o-mini:
    rpm: 500
    tpm: 200000
  anthropic/claude-3-5-haiku:
    rpm: 250
    tpm: 100000
  google/gemini-2.0-flash:
    rpm: 1000
    tpm: 1000000
```

Each entry becomes a `RateLimitEntry(rpm=..., tpm=...)` in `Config.rate_limits` keyed exactly `"openai/gpt-4o-mini"`, etc. The map is loaded once at boot and passed to `RedisTokenBucket(state, limits=cfg.rate_limits)`. SIGHUP-driven config reload (see [`config.md`](config.md)) builds a new `Config` and a new bucket if the relevant fields changed.
