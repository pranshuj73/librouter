# Request data plane: POST /v1/chat/completions

The hot path of the gateway. One HTTP POST in, one OpenAI-compatible JSON body out, with one or more vendor calls in between. This document follows a single request from socket bytes to response bytes, naming every file:line that mutates state, every error code it can produce, and every Prometheus collector it touches.

For the boot-time wiring that makes any of this possible see [`modules/app.md`](modules/app.md). For the failover loop itself see [`modules/router.md`](modules/router.md). For the weight cache the loop reads from see [`modules/routing.md`](modules/routing.md).

---

## At a glance

```
 1. FastAPI body validation          → ChatCompletionRequest         (gateway/models.py:146)
 2. Bearer-token resolution           → Caller                       (gateway/app.py:241)
 3. Daily-token-cap check             → Postgres SUM                 (gateway/app.py:366)
 4. Deadline armed; request_id minted → uuid4().hex                  (gateway/router.py:138,150)
 5. Repick loop start                 ─┐
    5a. WeightEngine.pick              │ in-process, no I/O          (gateway/router.py:169)
    5b. Bucket.try_acquire             │ Redis Lua, atomic           (gateway/router.py:175)
    5c. Vendor.chat with per-attempt   │ network call to OpenAI etc. (gateway/router.py:193)
        timeout
    5d. record_success / record_failure│ Redis hash, fire-and-forget (gateway/router.py:211/228)
    5e. on retryable error, exclude   ─┘ and loop                    (gateway/router.py:223-225)
 6. Success: build ChatCompletionResponse (response.id = request_id) (gateway/router.py:243)
 7. AttemptRecord ⇢ AccountingQueue (one per attempt, success+fail)  (gateway/app.py:401-409)
 8. REQUESTS_TOTAL / REQUEST_LATENCY / ATTEMPTS_TOTAL / COST_USD_TOTAL
 9. JSON response to client
```

The body of step 5 is the failover loop. It runs at most ~10 times in practice (one per healthy candidate) and exits as soon as one vendor returns success, a non-retryable caller error, or the deadline drains.

---

## Endpoint inventory

| Endpoint | Auth | Notes |
|---|---|---|
| `POST /v1/chat/completions` | Bearer (caller key, HMAC-SHA256 + pepper) | The hot path documented below. |
| `GET /v1/usage` | Bearer (caller key) | Caller-scoped; the `?caller=` query param is accepted but ignored (`app.py:342-354`, commit `8e046e5`/cr-1 §3.2). |
| `GET /metrics` | Bearer (`GATEWAY_METRICS_TOKEN` via `SecretsManager`) | Fail-closed if token unset; constant-time compare (`app.py:269-294`, commit `8e046e5`). |
| `GET /readyz` | none | Returns `{"status":"ready"}` only — tier names no longer disclosed (`app.py:262-266`, commit `84e5e64` / cr-1 §8.3). |
| `GET /healthz` | none | `{"status":"ok"}`. |

Migrations and caller seeding no longer run inside lifespan; ops invokes `scripts/apply_migrations.py` and `scripts/seed_callers.py` (orchestrated by `scripts/setup.sh`) before booting the app (commit `40eb4f6`). Lifespan only does `db.connect()` and, in real mode, warns at startup when the Postgres DSN lacks `sslmode=require` or the Redis URL is not `rediss://` (`app.py:122-132`, commit `196bf73`).

## Step-by-step

### 1. FastAPI body validation

- **File:line:** `gateway/app.py:357-362` (the endpoint signature); validation runs inside Pydantic on `ChatCompletionRequest` at `gateway/models.py:146-193`.
- **Reads:** the JSON body off the wire.
- **Writes:** nothing.
- **Errors produced:** any `pydantic.ValidationError` becomes HTTP 422 via FastAPI's default handler. This covers `stream=true` (rejected at `models.py:159`), `messages` length out of `[1, 512]`, `max_tokens` out of `(0, 16384]`, `temperature` / `top_p` out of range, metadata over 16 entries, per-key `len(key) > 64` / per-value `len(value) > 256`, per-message `content > 200_000` chars, and aggregate content `≥ 1_000_000` chars (`models.py:186-193`).
- **Metrics:** none. The validation rejection short-circuits before the endpoint body runs, so neither `REQUESTS_TOTAL` nor `REQUEST_LATENCY` is incremented for 422s.

### 2. Bearer-token resolution

- **File:line:** `_resolve_caller` at `gateway/app.py:241-251`; the lookup itself is `CallerResolver.resolve_bearer` in `gateway/auth.py`.
- **Reads:** the `Authorization` header; the in-process auth cache (60s TTL); on miss, the `callers` table in Postgres. Keys are matched by HMAC-SHA256 of the plaintext with the server-side pepper from `SecretsManager.get("GATEWAY_KEY_HASH_PEPPER")` (commit `4bcccd4`).
- **Writes:** the in-process auth cache.
- **Errors:** missing or unknown bearer raises `HTTPException(401, {"type": "auth", ...})`. Disabled callers also resolve to `None` → 401.
- **Metrics:** none on the 401 path.

### 3. Daily-token-cap check

- **File:line:** `gateway/app.py:366-377`.
- **Reads:** `Caller.daily_token_cap` (cached); `Database.caller_tokens_used_today(caller.name)` which executes a `SUM(input_tokens + output_tokens) FROM requests WHERE caller=$1 AND created_at >= date_trunc('day', now())`.
- **Writes:** nothing.
- **Errors:** if `used >= cap` raises `HTTPException(429, {"type": "caller_rate_limit", "retryable": False})`.
- **Metrics:** none on the 429 path (the per-caller cap fires before the router runs).
- **Note:** the cap is checked once per request, not re-checked between attempts. A burst can therefore overshoot by one request's worth of tokens. This is by design — see `cr-1.md` §4.1 if it's still a concern.

### 4. Deadline armed and request prep

- **File:line:** `gateway/router.py:138-152`.
- **Reads:** `Router._now()` (default `time.monotonic`), `req.metadata.request_id` for the client trace id.
- **Writes:** `deadline`, `exclude: set[CandidateRef]`, `tried`, `attempts`, computes `est = estimate_tokens(prompt_chars, max_tokens)` from `gateway/ratelimit.py`, mints `request_id = uuid4().hex` (`router.py:150`), and copies `metadata.request_id` (truncated to 128 chars) into `client_trace_id` (`router.py:151-152`). The caller-supplied `metadata.request_id` is **never** used as the response id; it is stored separately as `AttemptRecord.client_trace_id` on every attempt row (migration `0002_client_trace_id.sql`, commit `e74a3f3`).
- **Errors:** unknown tier raises `RouterError(INVALID_REQUEST)` before the loop is entered (`router.py:127-136`).
- **Metrics:** the `INVALID_REQUEST` outcome is recorded by the endpoint at `app.py:388-391` after the exception is caught.

### 5. The failover loop

The loop body (`router.py:154-259`) executes up to once per candidate in the tier. Each iteration first checks the deadline, then picks, then acquires the bucket, then calls the vendor.

#### 5a. Deadline check

- **File:line:** `router.py:155-167`.
- If `remaining < 1.5s` and at least one attempt has been made, raises `RouterError(DEADLINE_EXCEEDED)`. If no attempt has been made yet (the very first iteration drained too fast — should never happen unless clock skews), falls through to the post-loop `UPSTREAM_UNAVAILABLE` error.

#### 5b. WeightEngine.pick

- **File:line:** `router.py:169-173`; the engine itself in `gateway/routing/weights.py`.
- **Reads:** `cfg.tiers[tier]` (the candidate list), the engine's in-process `_cache: dict[CandidateRef, CandidateSignals]`, the `exclude` set, `self._rng`.
- **Writes:** nothing. The engine builds a fresh weight list each call.
- **Errors:** none — returns `None` if every candidate is excluded or has weight 0.
- **Metrics:** none directly; `ROUTING_WEIGHT` is updated on `/metrics` scrape via `_refresh_observability_gauges` (`app.py:297`).

A `cand is None` result exits the loop and falls through to `UPSTREAM_UNAVAILABLE` (`router.py:261-269`).

#### 5c. Bucket.try_acquire

- **File:line:** `router.py:175-181`; the Lua script is invoked via `RedisTokenBucket.try_acquire`.
- **Reads/writes:** the Redis hash `gw:rl:{provider}/{model}` — atomic refill-then-deduct.
- **Errors:** Redis unreachable → the Redis client raises → propagates as 500. The router does *not* catch `redis.RedisError`; see [`modules/ratelimit.md`](modules/ratelimit.md) and `cr-1.md` §6.2.
- **Metrics:** `BUCKET_REMAINING` is gauged on scrape, not per call.
- **Outcome:** on `ok=False` the candidate is appended to `tried` as `bucket_empty`, added to `exclude`, and the loop continues. No attempt is recorded — `AttemptRecord` is only emitted when a vendor was actually called.

#### 5d. Per-attempt timeout

- **File:line:** `router.py:183-185`.
- `attempt_timeout = min(max(0.1, remaining - buffer), per_attempt_max_s)`. With defaults that's `min(max(0.1, remaining - 0.5), 8.0)`. Cannot exceed 8s; floors at 100ms.

#### 5e. Vendor.chat

- **File:line:** `router.py:186-195`.
- **Reads:** `self._vendors[cand.provider]`, the request `messages`, `params`, and `attempt_timeout`.
- **Writes:** the vendor adapter performs HTTP I/O to the upstream LLM provider.
- **Errors caught:** anything in the `ProviderError` hierarchy (`gateway/errors.py`). Anything else (e.g. `httpx.ConnectError` if the adapter forgot to translate) propagates as 500 — this is a contract violation by the adapter.
- **Vendor missing:** if a tier references a provider that isn't in `self._vendors` (e.g. real-mode startup with no API key), the candidate is silently skipped with `tried = ('vendor_missing',)` and added to `exclude` (`router.py:187-191`). This makes "I forgot to set `GOOGLE_API_KEY`" a router-level skip rather than a 500.

#### 5f. On ProviderError

- **File:line:** `router.py:196-225`.
- **Status mapping:** `_STATUS_FOR_ERROR_KIND` (`router.py:79-86`) turns the exception class into the `status` string written into `AttemptRecord`.
- An `AttemptRecord` (carrying the gateway-generated `request_id` and the caller's `client_trace_id`) is appended to `attempts` and fire-and-forgotten to `Observer.record_failure` (`router.py:211`). The observation key is `gw:obs:{p}:{m}:{epoch_sec}`; see [`modules/routing.md`](modules/routing.md).
- **Caller errors:** `BadRequest`, `AuthError`, `ContentFiltered` are *not* retried. `caller_error_for(e)` from `gateway/errors.py` produces a fixed canonical message (raw vendor text stays in `vendor_detail` for logs only). The router then raises `RouterError(INVALID_REQUEST)` (HTTP 400) or `RouterError(AUTH)` (HTTP 401).
- **Retryable errors:** `RateLimited`, `Transient5xx`, `Timeout` fall through to the `continue` at line 225. The candidate is excluded and the loop repicks.
- **Metrics:** `ATTEMPTS_TOTAL{provider, model, status=...}` is incremented later in the endpoint at `app.py:401-404` once the result is in hand.

#### 5g. On success

- **File:line:** `router.py:227-259`.
- The `AttemptRecord` is built with `status="ok"`, real token counts, and the vendor's request id; `request_id` is the gateway-generated uuid4 hex and `client_trace_id` carries the caller's trace handle.
- `Observer.record_success(cand, latency_s=elapsed)` is awaited at line 228. This writes `successes`, `latency_sum_ms`, `latency_count` to the same `gw:obs:` hash, with a 4× window TTL.
- A `ChatCompletionResponse` is assembled with `id=request_id` (the gateway uuid4 hex — not the caller's value), one choice, role=assistant, and returned wrapped in `RouterResult(response, attempts)`.

### 6. Post-router accounting

- **File:line:** `gateway/app.py:401-409`.
- For each `AttemptRecord` in `result.attempts` (one per attempt, including failed ones):
  - `ATTEMPTS_TOTAL.labels(provider, model, status).inc()`.
  - If `status == "ok"`, `COST_USD_TOTAL.labels(caller, tier, provider).inc(cost_usd)`. The cost was computed in `Router._record` at `router.py:288-305`.
  - `accounting.enqueue(a)` — fire-and-forget into the `AccountingQueue`. Bounded deque; oldest record is dropped on overflow and `ACCOUNTING_DROPPED` is incremented live (commit `8e046e5`).

### 7. Top-level metrics + response

- **File:line:** `gateway/app.py:395-411`.
- `REQUESTS_TOTAL.labels(caller, tier=body.model, outcome="ok").inc()`.
- `REQUEST_LATENCY.labels(tier, outcome="ok").observe(elapsed)` where `elapsed = monotonic() - t0` and `t0` was captured at `app.py:382` just before `router.route(...)`.
- The `ChatCompletionResponse` (with `id` = gateway uuid4 hex) is returned; FastAPI serializes it.

---

## Error paths

Every HTTP status the endpoint can return:

| Status | Trigger | Source |
|---|---|---|
| 200 | Successful vendor call within deadline. | `app.py:411` |
| 400 | `ChatCompletionRequest` Pydantic validation failure. | FastAPI default; rules at `models.py:146-193` |
| 400 | Unknown tier — `body.model` not in `cfg.tiers`. | `router.py:127` → `RouterError(INVALID_REQUEST)` → `app.py:414-420` |
| 400 | Vendor `BadRequest` (e.g. context too long, malformed). | `errors.py` via `router.py:213-221` |
| 400 | Vendor `ContentFiltered`. | `errors.py` |
| 401 | Missing or unknown bearer token. | `app.py:246-250` |
| 401 | Missing/invalid `/metrics` bearer (`GATEWAY_METRICS_TOKEN`). | `app.py:283-290` |
| 401 | Vendor `AuthError` (our key was rejected upstream — operator misconfig). | `errors.py` |
| 422 | Pydantic validation produced a non-400 shape (FastAPI default for body model errors). | FastAPI default |
| 429 | Per-caller daily token cap exhausted. | `app.py:369-377` |
| 503 | All candidates exhausted before deadline (`UPSTREAM_UNAVAILABLE`). Includes "every candidate's bucket dry" and "every candidate's breaker open". | `router.py:261-269` |
| 504 | Deadline drained mid-failover (`DEADLINE_EXCEEDED`). At least one attempt was made before the budget ran out. | `router.py:158-166` |
| 500 | Anything not in the `ProviderError` hierarchy escaping the router (e.g. Redis unreachable mid-attempt, vendor adapter raised the wrong type, unexpected `KeyError` from a config gap). | FastAPI default; `cr-1.md` §6.2 |

The body for 400/401/503/504 is the JSON dump of `ErrorBody(type, message, retryable)` from `gateway/models.py:204-207`. `retryable` is `True` for `DEADLINE_EXCEEDED` and `UPSTREAM_UNAVAILABLE`, `False` for caller errors.

---

## Latency budget

Three knobs control the deadline, all on `Router.__init__` at `router.py:104-106`:

| Knob | Default | Meaning |
|---|---|---|
| `total_budget_s` | 10.0 | Wall-clock deadline for the whole request, from the moment `route()` is entered. |
| `per_attempt_max_s` | 8.0 | Upper bound on any single vendor call. |
| `deadline_buffer_s` | 0.5 | Slack reserved before the deadline to marshal a response or a `DEADLINE_EXCEEDED` body. |

The per-attempt timeout each iteration:

```
remaining       = deadline - now()
attempt_timeout = min(max(0.1, remaining - 0.5), 8.0)
```

Worked examples:

| `remaining` | `attempt_timeout` |
|---|---|
| 10.0 (first attempt) | `min(max(0.1, 9.5), 8.0) = 8.0` |
| 3.0 | `min(2.5, 8.0) = 2.5` |
| 1.5 (loop pre-check trips at exactly this) | not reached — `DEADLINE_EXCEEDED` raised first |
| 0.4 (deadline already drained, but pre-check missed by clock jitter) | `min(max(0.1, -0.1), 8.0) = 0.1` |

The `remaining < 1.5` guard at `router.py:156` is intentionally conservative: it ensures the next attempt has at least 1s of real work plus 0.5s buffer rather than being scheduled with a sub-second timeout that will inevitably trip.

---

## Sequence diagram

```
Client          FastAPI app          Router            WeightEng        Bucket(Redis)     Vendor          Observer(Redis)   Accounting
  │                 │                   │                  │                  │              │                  │                │
  │── POST /v1/chat │                   │                  │                  │              │                  │                │
  │   completions ─▶│                   │                  │                  │              │                  │                │
  │                 │── Pydantic ──▶ ok │                  │                  │              │                  │                │
  │                 │── resolve_bearer ▶│  Caller          │                  │              │                  │                │
  │                 │── cap check ──▶ db│                  │                  │              │                  │                │
  │                 │── router.route ──▶│                  │                  │              │                  │                │
  │                 │                   │── pick(tier) ───▶│                  │              │                  │                │
  │                 │                   │◀── CandidateRef  │                  │              │                  │                │
  │                 │                   │── try_acquire ───┼─────────────────▶│              │                  │                │
  │                 │                   │◀── ok=True       │                  │              │                  │                │
  │                 │                   │── vendor.chat ───┼──────────────────┼─────────────▶│                  │                │
  │                 │                   │                  │                  │              │                  │                │
  │                 │                   │      [ retryable ProviderError ]    │              │                  │                │
  │                 │                   │◀── raise ────────┼──────────────────┼──────────────│                  │                │
  │                 │                   │── record_failure ┼──────────────────┼──────────────┼─────────────────▶│                │
  │                 │                   │── exclude ─┐     │                  │              │                  │                │
  │                 │                   │            │                                                                            │
  │                 │                   │── pick (again) ▶ │  next candidate                                                      │
  │                 │                   │── try_acquire ───┼─────────────────▶│                                                   │
  │                 │                   │── vendor.chat ───┼──────────────────┼─────────────▶│                                    │
  │                 │                   │◀── ChatResult ───┼──────────────────┼──────────────│                                    │
  │                 │                   │── record_success ┼──────────────────┼──────────────┼─────────────────▶│                 │
  │                 │◀── RouterResult ──│                  │                  │              │                  │                 │
  │                 │── for a in attempts: enqueue ⇢ ──────┼──────────────────┼──────────────┼──────────────────┼────────────────▶│
  │                 │── REQUESTS_TOTAL.inc()                                                                                       │
  │                 │── REQUEST_LATENCY.observe()                                                                                  │
  │◀── 200 JSON ────│                                                                                                              │
```

`⇢` denotes a non-blocking enqueue; the AccountingQueue flushes in its own task (every 250ms or 200 rows; see [`modules/accounting.md`](modules/accounting.md)). The "repick" loop between the two `vendor.chat` rows can run multiple times until success, non-retryable error, deadline drain, or every candidate is excluded.
