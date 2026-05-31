# Internal LLM Gateway — Design

## Context

Six internal backend services currently each call OpenAI/Anthropic/Google directly. That couples each caller to vendor SDKs, makes provider failures user-visible, scatters API keys, and gives no central view of spend. We want a single internal service all backends route LLM calls through, so we can:

- Swap or fail over between vendors for the same logical tier (`fast` / `smart`) without caller code changes
- Absorb provider 429/5xx/timeouts when reasonable instead of propagating them
- Account spend by caller (currently we have no per-team attribution)
- Keep the system small enough that one on-call engineer can run it

Workload is small: peak 5–20 RPS, p95 budget 10s end-to-end, 6 internal callers, 3 providers each with their own per-minute rate limits. Decisions throughout favor "boring and debuggable" over "scalable to 10k RPS." The repo is greenfield Python 3.14 (`pyproject.toml`, `main.py` hello-world only).

User confirmed three foundational choices: **OpenAI-compatible request schema**, **non-streaming in v1**, **thin in-house adapter layer** over vendor SDKs.

## Goals / Non-goals

**In scope (v1)**
- Single HTTP endpoint that accepts OpenAI Chat Completions requests with logical model names
- Multi-vendor failover within a tier on retryable errors, inside a 10s deadline
- Provider-side rate-limit awareness (token-bucket per `(provider, model)`)
- Per-caller authentication and per-request spend record in Postgres
- One Grafana dashboard and a handful of alerts an on-call can act on

**Out of scope (v2+)**
- Streaming (SSE). Stub the route; return 400 if `stream=true`.
- Multi-region HA. Single region, 1–2 replicas.
- Prompt caching, semantic caching, response caching.
- Function/tool calling normalization. Pass-through where vendors support it; document quirks.
- Image/audio modalities.

## Architecture

```
                  +-----------+
                  |  Caller   |  (OpenAI SDK, base_url=gateway)
                  +-----+-----+
                        |  Bearer <caller_api_key>
                        v
+-----------------------+-----------------------+      +---------+
|                  Gateway (FastAPI)            | <--> |  Redis  |
|                                                |      | buckets |
|  auth -> router(weighted autorouting) -> Vendor|      | breakers|
|             |    ^                             |      | weights |
|             |    |  per-attempt timeout from   |      +---------+
|             |    |  remaining wall-clock        |
|             |    |                              |
|             v    |                              |
|       weight engine  <-- background refresh ----+
|       (health, budget, base weight)             |
|                                                  |
|  async fire-and-forget -> Postgres (spend log)   |
|  /metrics (Prometheus) ; structlog -> stdout     |
+--------------------------------------------------+
                        |
              +---------+---------+
              v                   v
         Postgres              Vendors
        (requests,           (openai/anthropic
         callers)             /google APIs)
```

Two stateless FastAPI replicas behind an LB. **Redis** holds all shared routing state — rate-limit buckets, breaker sample counters, rolling provider-health stats, and computed weights — so replicas make consistent routing decisions without coordination. Postgres holds durable state (spend log, caller table). Local memory holds only short-TTL caches of Redis-derived weights, refreshed by a 1s background task.

## API surface

Single endpoint, OpenAI-compatible:

```
POST /v1/chat/completions
Authorization: Bearer <caller_api_key>
Content-Type: application/json

{
  "model": "fast" | "smart",          # logical tier, not a vendor model
  "messages": [...],
  "max_tokens": 1024,
  "temperature": 0.2,
  "stream": false,                     # v1: must be false
  "metadata": {"request_id": "..."}    # optional caller-supplied id
}
```

Response mirrors OpenAI's shape so callers' existing SDKs work by changing `base_url`. The `model` field in the response is rewritten back to the tier name (e.g. `"fast"`) so callers don't accidentally couple to whichever vendor served them.

Also expose: `GET /healthz`, `GET /readyz`, `GET /metrics` (Prometheus), `GET /v1/usage?caller=...&since=...` (spend rollup).

Error mapping back to callers:
| Cause | Status | Body |
|---|---|---|
| Bad request from caller | 400 | error.type=`invalid_request` |
| Missing/invalid API key | 401 | error.type=`auth` |
| Caller daily-cap exceeded | 429 | error.type=`caller_rate_limit` |
| All candidates failed (after failover) | 503 | error.type=`upstream_unavailable`, `retryable: true` |
| Deadline exceeded mid-failover | 504 | error.type=`deadline_exceeded` |

## Tier configuration

Tiers, candidates, **base weights**, and routing parameters live in `config.yaml` (loaded at boot; SIGHUP reloads). There is no preference order — selection is weighted-random, adjusted dynamically by health and budget signals (see [Provider autorouting](#provider-autorouting)).

```yaml
tiers:
  fast:
    - { provider: anthropic, model: claude-haiku-4-5, weight: 50 }
    - { provider: openai,    model: gpt-4o-mini,      weight: 30 }
    - { provider: google,    model: gemini-2.5-flash, weight: 20 }
  smart:
    - { provider: anthropic, model: claude-sonnet-4-6, weight: 40 }
    - { provider: openai,    model: gpt-4o,            weight: 40 }
    - { provider: google,    model: gemini-2.5-pro,    weight: 20 }

routing:
  refresh_interval_ms: 1000      # how often the background task recomputes weights from Redis
  health_window_s: 60            # rolling window for error rate + p95
  target_latency_s: 3.0          # used to normalize latency into [0,1]
  min_weight_floor: 0.02         # if effective weight < floor, treat as 0 (degraded out)
  rng_seed_env: GATEWAY_RNG_SEED # optional, for reproducible tests

prices:                          # USD per 1M tokens
  anthropic/claude-haiku-4-5:  { input: 1.00, output: 5.00 }
  ...

rate_limits:                     # per-minute, fleet-wide (Redis enforces globally)
  anthropic/claude-sonnet-4-6: { rpm: 1000, tpm: 200000 }
  ...

callers:
  - { name: search-svc, key_hash: "sha256:...", daily_token_cap: 10_000_000 }
  - ...
```

Why YAML in repo (not DB-backed): six callers, three vendors, low change rate. A PR is the right change-management surface. Hot reload via SIGHUP covers urgent ops (e.g. drop a candidate's `weight` to 0 mid-incident).

## Provider abstraction

Abstract `Vendor` base class in `gateway/providers/base.py`, with one concrete subclass per real vendor and one mock subclass per vendor under `gateway/providers/mock/`. Mock vendors are the default in dev and tests; real vendors are opt-in via config.

```python
# gateway/providers/base.py
class Vendor(ABC):
    name: str                                       # "openai" | "anthropic" | "google"

    def __init__(self, secrets: SecretsManager): ...

    @abstractmethod
    async def chat(
        self,
        model: str,
        messages: list[Message],
        params: ChatParams,
        timeout_s: float,
    ) -> ChatResult: ...
    # ChatResult (in models.py): text, finish_reason, input_tokens, output_tokens, vendor_request_id
```

Concrete subclasses:

- `gateway/providers/openai.py` → `OpenAIVendor(Vendor)` — wraps `openai` SDK
- `gateway/providers/anthropic.py` → `AnthropicVendor(Vendor)` — wraps `anthropic` SDK
- `gateway/providers/google.py` → `GoogleVendor(Vendor)` — wraps `google-genai` SDK

Mock subclasses (used by default in dev and all tests):

- `gateway/providers/mock/openai_mock.py` → `MockOpenAIVendor(Vendor)`
- `gateway/providers/mock/anthropic_mock.py` → `MockAnthropicVendor(Vendor)`
- `gateway/providers/mock/google_mock.py` → `MockGoogleVendor(Vendor)`

Each mock supports a programmable script of responses keyed by `(model, prompt_hash)` with injectable: artificial `latency_s`, error sequence (e.g. `[RateLimited, RateLimited, ok]`), deterministic token counts, deterministic `vendor_request_id`. This makes failover / breaker / bucket tests trivially reproducible without `respx`-style HTTP mocking.

Vendor selection at boot:

```yaml
# config.yaml
provider_mode: mock        # mock | real          (env var override: GATEWAY_PROVIDER_MODE)
```

The router talks only to `Vendor` instances; whether they're real or mock is invisible to it.

Vendors own:
- Translating to/from vendor schema (each real vendor is ~150 LOC)
- Pulling `input_tokens`/`output_tokens` out of vendor responses
- Mapping vendor errors to a normalized `ProviderError` taxonomy: `RateLimited`, `Transient5xx`, `Timeout`, `BadRequest`, `Auth`, `ContentFiltered`

Vendors do not retry, do not look at circuit breakers, do not touch the rate-limit bucket. That logic lives in the router so failover behavior is uniform.

## Secrets management

Abstract `SecretsManager` in `gateway/secrets.py` with two implementations:

```python
class SecretsManager(ABC):
    @abstractmethod
    def get(self, key: str) -> str: ...     # raises KeyError on miss
    @abstractmethod
    def has(self, key: str) -> bool: ...

class EnvSecretsManager(SecretsManager):
    """Production. Reads from process env, e.g. OPENAI_API_KEY."""

class MockSecretsManager(SecretsManager):
    """Dev/tests. In-memory dict seeded from a fixture or test setup."""
    def __init__(self, seed: dict[str, str] | None = None): ...
    def set(self, key: str, value: str) -> None: ...
```

Selected at boot from config (`secrets_mode: mock | env`, env override `GATEWAY_SECRETS_MODE`). Real vendor classes receive the manager via constructor injection so tests can swap it freely. Caller-API-key hashes still live in Postgres — `SecretsManager` is only for **outbound** vendor credentials.

## Pydantic models — single file

All Pydantic models live in `gateway/models.py` — no per-module model definitions. This gives one place to scan the full data shape of the system. Contents:

- **Config models**: `Config`, `TierEntry`, `PriceEntry`, `RateLimitEntry`, `CallerEntry`
- **Wire models** (OpenAI-compatible request/response): `ChatCompletionRequest`, `ChatCompletionResponse`, `Choice`, `Message`, `Usage`, `ErrorBody`
- **Internal DTOs**: `ChatParams`, `ChatResult`, `AttemptRecord`, `CandidateRef`, `Caller`
- **Error taxonomy enum**: `ProviderErrorKind`

Non-Pydantic types (abstract bases, Protocols, exceptions, dataclasses without validation) stay in their own modules. The `Vendor`/`SecretsManager` ABCs do not go in `models.py`.

## Provider autorouting

Selection across candidates is **weighted-random**, not ordered. Every candidate in a tier has a base weight (config); each request the router computes an **effective weight** by multiplying base weight by two signals derived from recent observed behavior:

```
effective_weight(p, m) = base_weight(p, m) * health_score(p, m) * budget_score(p, m)

health_score = (1 - error_rate_last_60s) * (target_latency / (target_latency + p95_latency_last_60s))
budget_score = min(rpm_remaining/rpm_capacity, tpm_remaining/tpm_capacity)

if breaker(p, m).is_open() OR effective_weight < min_weight_floor:
    effective_weight = 0
```

A candidate that's healthy and lightly loaded gets near its base weight. A candidate that's slow or errorful sees `health_score` collapse; one near its rate limit sees `budget_score` collapse. Both clamp to 0 cleanly, which is what feeds the soft-degradation behavior an on-call wants.

**Where the signals come from (Redis, refreshed once/sec into a per-replica cache):**

- `error_rate` and `p95_latency`: a 60-second sliding window of per-(provider, model) outcomes, stored as 60 one-second Redis hash buckets keyed `gw:obs:{p}:{m}:{epoch_sec}` with `successes`, `failures`, and an HDR-style latency histogram serialized into the hash. Increments are pipelined into Redis fire-and-forget; the background refresher reads the last 60 keys and computes the aggregate.
- `rpm_remaining` / `tpm_remaining`: from the Redis-backed token bucket's current value (one `GET` per dim).
- `breaker state`: from `gw:brk:{p}:{m}` (a Redis hash with `state`, `opened_at`, `failures`, `samples`).

A 1-second refresh cadence is fine: routing decisions are stable over a second of traffic at 20 RPS, and a 1s lag on degradation is well inside the 10s SLO budget.

**Selection algorithm** (`gateway/routing/weights.py`):

```python
def pick(tier: str, exclude: set[CandidateRef], rng: Random) -> CandidateRef | None:
    cands = [c for c in tier_candidates(tier) if c not in exclude]
    weights = [effective_weight_cache[c] for c in cands]
    total = sum(weights)
    if total <= 0:
        return None
    r = rng.random() * total
    acc = 0.0
    for c, w in zip(cands, weights):
        acc += w
        if r <= acc:
            return c
    return cands[-1]                                     # numerical-edge safety
```

The RNG is seedable via `GATEWAY_RNG_SEED` so tests can assert exact picks; in production it's `random.SystemRandom()`.

## Routing & failover

The hot path uses weighted selection adaptively — on each failure, the failed candidate joins `exclude` and we pick again from the remaining ones. This naturally avoids ordered "always retry OpenAI second" patterns and lets a degraded provider receive proportionally less traffic over time without manual intervention.

```python
async def route(req, caller):
    deadline = monotonic() + 10.0
    exclude: set[CandidateRef] = set()
    tried: list[tuple[CandidateRef, str]] = []

    while True:
        remaining = deadline - monotonic()
        if remaining < 1.5:                              # not worth another attempt
            break
        cand = weights.pick(req.tier, exclude, rng)
        if cand is None:                                 # no viable candidates left
            break
        if not await bucket.try_acquire(cand, est_tokens(req)):
            tried.append((cand, "bucket_empty"))
            exclude.add(cand); continue

        attempt_timeout = min(remaining - 0.5, 8.0)
        try:
            t0 = monotonic()
            result = await vendors[cand.provider].chat(
                cand.model, req.messages, req.params, attempt_timeout,
            )
            elapsed = monotonic() - t0
            await observe.record_success(cand, elapsed, result)
            accounting.enqueue(req, caller, cand, result, elapsed)
            return as_openai_response(result, tier=req.tier)
        except (RateLimited, Transient5xx, Timeout) as e:
            await observe.record_failure(cand, type(e).__name__)
            tried.append((cand, type(e).__name__))
            exclude.add(cand); continue
        except (BadRequest, Auth, ContentFiltered) as e:
            return error_response_from(e)                # don't fail over

    return error_503(tried)
```

Key decisions:

- **Deadline-driven, not retry-count-driven.** Each attempt's timeout is computed from remaining wall-clock so we never blow the 10s budget. The 0.5s buffer covers our own overhead and response serialization.
- **Weighted-random first pick, then exclude-and-repick on failure.** The failed candidate is excluded *for this request* — we still record its failure into the rolling window, which downgrades its weight for *future* requests via the standard refresh cycle (no special-case logic).
- **Don't fail over on caller errors.** `BadRequest` returns 400 immediately. Failing over wastes budget and may succeed on a more lenient vendor, masking the bug.
- **Skip, don't queue, when the bucket is empty.** At 20 RPS we'd rather try another candidate than add queueing latency. If all candidates' buckets are empty, return 503 fast.
- **`observe.record_*` writes to Redis fire-and-forget.** The hot path never awaits a Redis write longer than ~1ms (pipeline + microsecond network in-cluster). If Redis is slow or down, observation writes are skipped (`asyncio.shield` with a 50ms timeout) — the request still completes.
- **The `tried` list is logged** so an on-call can replay which candidates were chosen and why each failed.

## Provider-side rate limiting

Redis-backed token bucket per `(provider, model)`, two dimensions (RPM, TPM) acquired atomically via a single Lua script. Bucket capacity is the **fleet-wide** vendor limit (e.g. 1000 RPM) — Redis is the single source of truth, so we don't divide by replica count or apply a per-replica safety margin. A 10% headroom is baked into the configured capacity.

```lua
-- ratelimit.lua: KEYS = {rpm_key, tpm_key}, ARGV = {now_ms, rpm_cap, tpm_cap, refill_per_ms_rpm, refill_per_ms_tpm, request_tokens}
-- Computes current bucket level via lazy refill, attempts to subtract 1 RPM + request_tokens TPM atomically.
-- Returns 1 on success and the post-decrement remaining for both dims; 0 on failure with the shortfall.
```

- `est_tokens = prompt_chars / 4 + max_tokens` — rough but consistent.
- Refill is continuous (lazy, computed from `now_ms - last_refill_ms` inside the Lua script).
- Vendor response headers (e.g. OpenAI's `x-ratelimit-remaining-*`) opportunistically **clamp** the bucket via a second Lua script when the vendor reports less than we think. Handles vendor-side dynamic limit changes.

Per-attempt cost: ~0.3ms (one local-network Redis call). Negligible against a 10s LLM budget.

## Circuit breakers

Per `(provider, model)`, sliding-window — sample counters in Redis hash `gw:brk:{p}:{m}`, with a per-replica in-process snapshot refreshed every 1s to keep the hot-path check sync and non-blocking:

- Closed → Open when failure rate ≥ 30% over the last 30s with ≥ 20 samples
- Open holds for 30s, then Half-open
- Half-open allows **one probe across the whole fleet** — implemented as a Redis `SET NX EX 10` lock on `gw:brk:{p}:{m}:probe`; whichever replica gets it sends the probe
- Success → Closed; failure → re-open
- Only `RateLimited` / `Transient5xx` / `Timeout` count as failures. Caller errors don't trip it.
- The 1s in-process snapshot trades up to 1s of stale state for zero hot-path Redis latency on the breaker check. State changes (open/close) are also published on a Redis pub/sub channel so replicas converge faster than the polling interval when something flips.

## Spend tracking & accounting

**Capture path (sync):** every successful attempt produces an `AttemptRecord`:

```
request_id, caller, tier, provider, model,
input_tokens, output_tokens, attempt_index, latency_ms,
status, vendor_request_id, ts
```

Cost computed inline from the price table. We record **every attempt, not just the winner** — failed attempts still cost budget (especially with vendors that bill on dropped streams) and they're how an on-call diagnoses failover storms.

**Write path (async):** an in-process bounded queue (size 10k) drained by a single background task doing batched `INSERT ... VALUES (...), (...)` every 250ms or 200 rows. If the queue ever fills (Postgres outage) we log loudly and drop oldest — we won't block caller responses on accounting. A `gateway_accounting_dropped_total` counter alerts on this.

**Schema** (`migrations/0001_init.sql`):

```sql
CREATE TABLE requests (
  id            BIGSERIAL PRIMARY KEY,
  request_id    UUID NOT NULL,
  caller        TEXT NOT NULL,
  tier          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  attempt_idx   SMALLINT NOT NULL,
  input_tokens  INT NOT NULL DEFAULT 0,
  output_tokens INT NOT NULL DEFAULT 0,
  cost_usd      NUMERIC(10,6) NOT NULL DEFAULT 0,
  latency_ms    INT NOT NULL,
  status        TEXT NOT NULL,        -- ok | rate_limited | transient_5xx | ...
  vendor_req_id TEXT,
  ts            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON requests (caller, ts DESC);
CREATE INDEX ON requests (ts DESC);

CREATE TABLE callers (
  name             TEXT PRIMARY KEY,
  key_hash         TEXT NOT NULL,
  daily_token_cap  BIGINT,
  enabled          BOOLEAN NOT NULL DEFAULT TRUE
);
```

No pre-aggregation. At 20 RPS × 86400s × 3 attempts worst-case ≈ 5M rows/day; manageable, and aggregations over a single day are sub-second with the index. Partition by week if it ever matters.

## Auth & caller identity

`Authorization: Bearer <key>` → SHA-256 → lookup in `callers`. Result cached in-process for 60s. Per-caller daily cap is checked against a small in-memory counter that's seeded on startup from a `SUM(input+output)` query for today, and incremented as we serve.

Per-caller RPS limit: out of scope for v1 (six callers, all internal; we trust them to behave). Add if a caller starts misbehaving.

## Deployment & ops

- **Stack:** FastAPI + uvicorn, vendor SDKs (`openai`, `anthropic`, `google-genai`), `asyncpg`, `structlog`, `prometheus-client`, `pyyaml`. All async.
- **Topology:** 1 replica to start, room for 2 behind a TCP/HTTP LB. Postgres = whatever the org already runs (RDS, managed PG).
- **Local Postgres:** `docker-compose.yml` runs `postgres:16` with a **named volume `gateway_pg_data`** mounted at `/var/lib/postgresql/data` so DB state persists across `down`/`up` cycles. Compose also bind-mounts `migrations/` to `/docker-entrypoint-initdb.d/` so the schema is created on first boot.
- **Secrets:** in production, `EnvSecretsManager` reads vendor keys from env (mounted from secrets manager). In dev/tests, `MockSecretsManager` holds throwaway values. Caller key hashes always in `callers` table; raw keys handed out once at onboarding.
- **Config rollout:** `config.yaml` in repo; deploys ship a new pod with new config. `kill -HUP` reloads in place for incident-time swaps.

### Observability (the part that makes one on-call viable)

**Structured logs**, one line per request, fields: `request_id`, `caller`, `tier`, `attempts: [{provider, model, status, latency_ms}]`, `winner`, `total_latency_ms`, `input_tokens`, `output_tokens`, `cost_usd`. This single line answers ~90% of "what happened with this request?" tickets.

**Prometheus metrics:**
- `gateway_requests_total{caller, tier, outcome}` — counter
- `gateway_request_latency_seconds{tier, outcome}` — histogram
- `gateway_attempts_total{provider, model, status}` — counter
- `gateway_routing_weight{provider, model}` — gauge (effective weight, post-refresh)
- `gateway_routing_score{provider, model, kind}` — gauge (kind=health|budget|base)
- `gateway_breaker_state{provider, model}` — gauge (0=closed, 1=half, 2=open)
- `gateway_bucket_remaining{provider, model, dim}` — gauge (dim=rpm|tpm)
- `gateway_redis_op_duration_seconds{op}` — histogram (op=ratelimit|observe|breaker|refresh)
- `gateway_redis_down` — gauge (0|1)
- `gateway_cost_usd_total{caller, tier, provider}` — counter
- `gateway_accounting_dropped_total` — counter

**One Grafana dashboard** with rows: traffic (RPS by caller, tier), latency (p50/p95/p99 vs 10s SLO line), failover health (attempts per request, breaker states), autorouting (live weights stacked area + score breakdown), provider health (error rate by `(provider, model)`), spend (USD/hr by caller), Redis health (op p95).

**Alerts** (Alertmanager → on-call):
1. p95 latency > 10s for 5m → page
2. 5xx/504 rate > 2% for 5m → page
3. Any breaker open > 10m → ticket
4. Accounting drops > 0 over 5m → ticket
5. Daily spend rate 3× last week's rate at same hour → ticket
6. Postgres pool exhaustion → page
7. `gateway_redis_down == 1` for 1m → page
8. Any tier with total effective weight == 0 for 1m → page (no viable candidates)

## Multi-replica considerations

Redis is the single source of truth for buckets, breaker sample counters, and the rolling observation window. Replicas can scale horizontally without rebalancing limits or losing breaker consistency. Per-replica in-process state is bounded to **read caches** with explicit ≤1s staleness:

- Weight cache: refreshed once/sec by a background task pulling aggregated stats from Redis.
- Breaker snapshot: refreshed once/sec + pub/sub push for state transitions.
- Rate-limit bucket: not cached locally — every `try_acquire` hits Redis (cheap, atomic).

If Redis is down, the gateway degrades gracefully: see [Failure modes](#failure-modes--explicit-handling).

## Failure modes — explicit handling

| Failure | Behavior |
|---|---|
| Provider 429 on first candidate | Fail over to next candidate; bucket clamped from response header |
| Provider 5xx persistent | Breaker opens after 30s of failures; routes skip it for 30s |
| Provider timeout | Counted as failure; failover if deadline allows, else 504 |
| All vendors down | 503 with `retryable: true` and the `tried` list in logs |
| Vendor returns malformed response | Treated as `Transient5xx`; failover |
| Caller sends bad request | 400, no failover, no breaker impact |
| Postgres down | Accounting queue absorbs; if it fills, drop + alert; requests still served |
| Redis down | Hot path falls back to: in-process token bucket seeded from config (lossy but safe — caps at fleet limit / replica count × 0.5 conservative), local breaker state, last cached weights frozen. `gateway_redis_down` gauge → page. Auto-resumes when Redis returns. |
| Redis slow (p99 > 50ms) | `try_acquire` and `record_*` calls are wrapped with 50ms timeout; on timeout the call returns "permitted" (rate-limit) or is dropped (observe). We log; the SLO holds. |
| Gateway pod crash | LB removes it; in-flight requests fail; client retries against survivor |
| Config typo | Pydantic validation fails at boot/reload; old config stays in effect on reload, pod refuses to start on boot |

## Files to create

```
gateway/
  app.py                  # FastAPI app, /v1/chat/completions, /healthz, /readyz, /metrics, /v1/usage
  models.py               # ALL Pydantic models (config, wire, internal DTOs) — single file
  auth.py                 # Bearer-key middleware -> Caller (with 60s cache)
  config.py               # YAML loader, validation against models.py, SIGHUP reload
  secrets.py              # SecretsManager ABC, EnvSecretsManager, MockSecretsManager
  redis_state.py          # async Redis client wrapper + loaded Lua scripts (ratelimit, clamp, breaker probe lock)
  routing/
    __init__.py
    weights.py            # effective-weight computation, weighted-random pick, RNG seeding
    observe.py            # record_success / record_failure -> sliding-window Redis writes
    refresh.py            # background task: aggregates Redis windows -> per-replica weight cache (1s)
  router.py               # adaptive failover loop using routing/* + Redis bucket + breaker snapshot
  ratelimit.py            # async Redis-backed token bucket (Lua-script callouts via redis_state)
  breaker.py              # Redis-backed sliding-window + local snapshot + pub/sub subscriber
  accounting.py           # Bounded queue + batched Postgres writer
  db.py                   # asyncpg pool + query helpers
  metrics.py              # Prometheus collectors
  logging.py              # structlog config (JSON to stdout)
  errors.py               # ProviderError taxonomy + API error response builders
  providers/
    __init__.py           # build_vendors(config, secrets) -> dict[str, Vendor]
    base.py               # Vendor ABC
    openai.py             # OpenAIVendor(Vendor)
    anthropic.py          # AnthropicVendor(Vendor)
    google.py             # GoogleVendor(Vendor)
    mock/
      __init__.py
      openai_mock.py      # MockOpenAIVendor(Vendor) — scripted responses, error/latency injection
      anthropic_mock.py
      google_mock.py
config.yaml               # tiers, prices, rate_limits, callers, provider_mode, secrets_mode
config.dev.yaml           # provider_mode: mock, secrets_mode: mock — used by docker-compose
migrations/
  0001_init.sql           # requests, callers (mounted into postgres initdb)
tests/
  conftest.py             # fixtures: MockSecretsManager, mock Vendors, in-mem Postgres pool
  test_models.py          # Pydantic validation contract tests
  test_secrets.py
  test_redis_state.py     # Lua-script atomicity, clamp, probe lock (against testcontainers Redis)
  test_ratelimit.py       # Redis-backed token bucket (uses test_redis_state fixtures)
  test_breaker.py         # Redis-backed sliding window + local snapshot + pub/sub
  routing/
    test_weights.py       # effective-weight math, seeded picks, weight=0 conditions
    test_observe.py       # writes increment expected Redis keys
    test_refresh.py       # cache equals aggregated Redis window
  test_router.py          # adaptive failover, exclude-and-repick, deadline-budget math, 503/504 cases
  test_accounting.py      # queue drop, batch flush
  providers/
    test_mock_vendors.py  # scripted-response contract per mock
    test_openai_vendor.py # real adapter against MockSecretsManager + recorded vendor fixtures
    test_anthropic_vendor.py
    test_google_vendor.py
  test_app_e2e.py         # FastAPI TestClient + docker-compose Postgres + mock vendors
Dockerfile
docker-compose.yml        # services: gateway, postgres, redis (named volumes for both)
dashboards/
  gateway.json            # provisioned Grafana dashboard
README.md                 # runbook: add a caller, rotate keys, swap a model, read the dashboard
```

`pyproject.toml` deps to add: `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `asyncpg`, `redis[hiredis]`, `structlog`, `prometheus-client`, `pyyaml`, `openai`, `anthropic`, `google-genai`. Dev: `pytest`, `pytest-asyncio`, `respx`, `freezegun`, `testcontainers[redis,postgres]`.

`docker-compose.yml` sketch (state-preserving for both PG and Redis):

```yaml
volumes:
  gateway_pg_data:
  gateway_redis_data:

services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: gateway
      POSTGRES_PASSWORD: gateway
      POSTGRES_DB: gateway
    ports: ["5432:5432"]
    volumes:
      - gateway_pg_data:/var/lib/postgresql/data
      - ./migrations:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "gateway"]
      interval: 2s
      timeout: 2s
      retries: 30

  redis:
    image: redis:7
    # AOF on for durability of the sliding-window observation state across restarts
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]
    ports: ["6379:6379"]
    volumes:
      - gateway_redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 2s
      retries: 30

  gateway:
    build: .
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    environment:
      GATEWAY_DB_DSN:     postgres://gateway:gateway@postgres:5432/gateway
      GATEWAY_REDIS_URL:  redis://redis:6379/0
      GATEWAY_CONFIG:     /app/config.dev.yaml
      GATEWAY_PROVIDER_MODE: mock
      GATEWAY_SECRETS_MODE:  mock
    ports: ["8000:8000"]
```

## Implementation order (TDD)

Build strictly tests-first. Each module below ships only when its tests are red → green → refactored. No module is started before the one above it is green. Mock vendors and `MockSecretsManager` exist precisely so this entire ladder is testable without any network or real credentials.

1. **`models.py`** — write `test_models.py` first: round-trip parsing, rejection of invalid configs (unknown provider in a tier, negative price, negative weight, missing caller key_hash), OpenAI-compatible request validation (`stream=true` must error in v1). Then implement the Pydantic models.
2. **`secrets.py`** — write `test_secrets.py`: `MockSecretsManager.set/get/has`; `EnvSecretsManager` reads from env with `monkeypatch`; `KeyError` on miss. Then implement.
3. **`redis_state.py`** — write `test_redis_state.py` against a testcontainers Redis: each Lua script loads, ratelimit Lua atomically decrements both RPM and TPM, clamp shrinks remaining, probe lock `SET NX EX` only one of N concurrent callers succeeds. Then implement the client wrapper and script loaders.
4. **`ratelimit.py`** — write `test_ratelimit.py` against the same testcontainers Redis: bucket starts at capacity, `try_acquire` consumes both dims, refills linearly with `freezegun`-controlled time injected into the Lua script via `ARGV[now_ms]`, `try_acquire` returns false when either dim short, clamp reduces remaining. Then implement.
5. **`breaker.py`** — write `test_breaker.py`: stays closed under threshold; opens at threshold; rejects in open state; half-open after window; closes on probe success; reopens on probe failure; caller errors don't count; pub/sub message flips local snapshot within 100ms. Then implement.
6. **`routing/observe.py`** — write `test_observe.py`: `record_success` increments expected per-second hash bucket; `record_failure` records error kind; histogram serialization round-trips. Then implement.
7. **`routing/weights.py`** — write `test_weights.py` (no Redis needed; pure math): `health_score` for {healthy, half-error, half-slow, fully-broken} candidates; `budget_score` from given (rpm_remaining, tpm_remaining); `effective_weight` zeros on breaker_open and below floor; `pick` distribution converges to weight ratio over 10k seeded trials; `pick` excludes given set; `pick` returns None when all weights zero. Then implement.
8. **`routing/refresh.py`** — write `test_refresh.py`: given a seeded Redis with synthetic observation buckets across 60s, the refresher's computed cache matches the expected aggregate `(error_rate, p95, rpm_remaining, tpm_remaining)`. Then implement the background task.
9. **`providers/base.py` + `providers/mock/*`** — write `test_mock_vendors.py`: scripted success; scripted error sequence (`[RateLimited, ok]`); injected `latency_s` respects `timeout_s` and raises `Timeout`; token counts deterministic; `vendor_request_id` deterministic. Then implement the ABC and three mocks. Real vendors come later (step 13).
10. **`router.py`** — write `test_router.py` using mock vendors + seeded RNG + fake-clock + testcontainers Redis: success on first weighted pick; `RateLimited` then exclude-and-repick succeeds on second; all candidates fail → 503 with `tried` list including all three; deadline-exceeded mid-loop → 504; breaker-open candidate yields weight 0 and isn't picked; empty-bucket candidate's `try_acquire` fails and we repick; `BadRequest` returns 400 immediately. Verify failed attempt's `observe.record_failure` reached Redis. Then implement.
11. **`accounting.py`** — write `test_accounting.py`: queue accepts up to capacity; oldest-dropped + `dropped_total` increments on overflow; batched flush single-statement; timer-bounded. In-process fake DB writer. Then implement.
12. **`db.py` + `migrations/0001_init.sql`** — write `test_db.py` against testcontainers Postgres: insert/select on `requests` and `callers`; `EXPLAIN` uses the index; migration idempotent across re-runs. Then implement.
13. **`auth.py`** — write `test_auth.py`: valid bearer → `Caller`; missing/invalid → 401; cache hit avoids second DB lookup within 60s; disabled caller → 401. Then implement.
14. **`app.py`** — write `test_app_e2e.py` using FastAPI `TestClient` against compose-style Postgres+Redis+mock vendors: full happy-path; failover path produces 2 `requests` rows with correct `cost_usd`; 503 path; daily-cap-hit returns 429; over a synthetic 1000-request workload the per-provider distribution approximates the configured weights to within 10%. Then wire up the FastAPI app.
15. **`providers/openai.py`, `anthropic.py`, `google.py`** — last. For each: contract tests that real adapter conforms to mock behavior (`isinstance(v, Vendor)`, returns `ChatResult`, maps vendor errors to taxonomy). `respx` stubs HTTP at the SDK transport level with one fixture per vendor recorded from a real call. Real adapters aren't needed for v1 against mocks — they unlock cutover.
16. **`metrics.py`, `logging.py`** — written incrementally alongside each module that emits metrics/logs; verified by `test_app_e2e.py` scraping `/metrics` and asserting `gateway_routing_weight{provider,model}` and `gateway_attempts_total` series exist.

### Test gates before merge
- `pytest` is green
- Coverage ≥ 85% for `router.py`, `routing/weights.py`, `routing/refresh.py`, `breaker.py`, `ratelimit.py`, `accounting.py` (the failover-critical paths)
- `docker compose up` then `curl localhost:8000/v1/chat/completions ...` returns a mock response end-to-end
- Weighted-distribution check: 1000 requests against mock vendors with weights 50/30/20 lands within ±10% of expected per-provider counts (seeded RNG OFF for this gate; assert statistically)
- `docker compose down && docker compose up` confirms Postgres data persists via `gateway_pg_data` (a caller row survives) AND Redis observation buckets persist via `gateway_redis_data` (a key written before `down` is still present after `up`)

## Pre-launch verification (beyond unit + e2e)

**Load (one-off, before launch)**
- Hit gateway at 30 RPS for 10 minutes with one mock vendor configured to return 429 20% of the time. Confirm p95 < 10s, no accounting drops, no task leaks.

**Pre-prod soak (1 week)**
- Mirror 1% of real caller traffic through the gateway in shadow mode (`provider_mode: real`, log only, don't return). Confirm error rate, latency distribution, and spend total within 10% of direct-call baseline before flipping callers over one at a time.

**Cutover** — flip callers one per day, monitor dashboard. Keep direct-call code paths behind a flag for 2 weeks so any caller can roll back in one config change.

IMPORTANT: USE docs/ to keep track of progress made. store PLAN in docs/PLAN.md
