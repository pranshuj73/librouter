# Architecture Overview

The Gateway is a stateless HTTP service that fronts multiple third-party LLM vendors (OpenAI, Anthropic, Google) behind one **OpenAI-compatible** `/v1/chat/completions` API. It exists so that internal callers can target *logical tiers* (`fast`, `smart`) and the gateway picks an actual provider per request based on health, budget, and configured weights — failing over silently when a vendor degrades.

This document gives the 10-minute big picture. Each subsystem is detailed in [`modules/`](modules/).

---

## 1. What the gateway does (and doesn't)

**It does:**
- Authenticate internal callers via bearer tokens (HMAC-SHA256 + server-side pepper, lookup in Postgres).
- Pick a `(provider, model)` candidate from a tier by weighted-random selection over computed weights (`base × health × budget`).
- Enforce fleet-wide rate limits per `(provider, model)` via an atomic two-dimensional token bucket in Redis.
- Trip a circuit breaker per `(provider, model)` once error rate over a sliding window passes a threshold; gate fleet-wide half-open probes.
- Fail over to the next-best candidate on retryable errors within a global deadline (10s default).
- Record every attempt (success or failure) as a row in Postgres via a batched, bounded write-behind queue.
- Enforce a per-caller daily token cap.
- Export Prometheus metrics for everything that matters: per-caller request count, per-attempt outcome, routing weights, breaker state, bucket remaining, USD cost, refresh-task errors, accounting drops.

**It does not (yet):**
- Stream responses (`stream=true` is rejected; see `models.py`).
- Run an admin / multi-tenant authorization layer (`/v1/usage` is bearer-gated but not caller-scoped — see [`cr-1.md` §3.2](../code-review/cr-1.md)).
- Terminate TLS (expected behind a load balancer; boot warns if Postgres/Redis transports are not TLS in real mode).
- Persist anything other than per-attempt rows + caller registry.

---

## 2. Component map

```
                           ┌─────────────────────────────────────────────┐
                           │            uvicorn / FastAPI app            │
                           │                gateway/app.py                │
                           │  (migrations + caller seeding NOT run here   │
                           │   — see scripts/ for pre-deploy steps)       │
                           └─────────────────────────────────────────────┘
                              │             │                 │
                  ┌───────────┘             │                 └────────────┐
                  ▼                         ▼                              ▼
        ┌──────────────────┐    ┌─────────────────────┐         ┌─────────────────────┐
        │  CallerResolver  │    │       Router        │         │ /metrics (auth-gated)│
        │  gateway/auth.py │    │  gateway/router.py  │         │ /healthz, /readyz    │
        │  (HMAC + pepper, │    │  (generates server- │         └─────────────────────┘
        │   LRU-bounded)   │    │   side request_id;  │
        └─────────┬────────┘    │   client trace kept │
                  │             │   in client_trace_id)│
                  │             └──────────┬──────────┘
                  │                        │
                  │             ┌──────────┼─────────────┬─────────────┬─────────────┐
                  ▼             ▼          ▼             ▼             ▼             ▼
            ┌──────────┐  ┌──────────┐ ┌────────┐  ┌─────────┐  ┌────────────┐ ┌────────────┐
            │ Database │  │WeightEng.│ │ Bucket │  │BreakerSt│  │  Vendors   │ │  Pricing   │
            │ (asyncpg)│  │ (in-proc)│ │(Redis) │  │(Redis)  │  │ (real/mock)│ │ (in-proc,  │
            └────┬─────┘  └────┬─────┘ └───┬────┘  └────┬────┘  └──────┬─────┘ │  immutable)│
                 │             ▲           │            │              │       └─────┬──────┘
                 │             │           │            │              │             │
                 │     ┌───────┴────────┐  │            │              │             │
                 │     │  RefreshTask   │  │            │              │             │
                 │     │ (every 1s,     │  │            │              │             │
                 │     │  jittered      │◄─┴────────────┘              │             │
                 │     │  backoff on    │                              │             │
                 │     │  failure)  ┄┄  │                              │             │
                 │     └────────────────┘                              │             │
                 │                                                     │             │
                 │     ┌──────────────────┐         ┌──────────────────┘             │
                 │     │   Observer       │◄────────┤  on success/fail                │
                 │     │ (Redis hash/sec) │         │  (latency, error kind)          │
                 │     └──────────────────┘         │                                 │
                 │                                  │                                 │
                 │     ┌─────────────────┐          │   ┌─────────────────────────┐  │
                 └────►│ AccountingQueue │◄─────────┘   │ cost_usd computed via   │◄─┘
                       │ (bounded deque, │              │ pricing.py (LiteLLM     │
                       │  live drop      │              │ vendored JSON)          │
                       │  counter)       │              └─────────────────────────┘
                       └─────────────────┘   ┄┄ flushes to Postgres
                                             ┄┄ every 250 ms or 200 rows
```

- **In-process singletons** (one per replica): `Router`, `WeightEngine`, `AccountingQueue`, `RefreshTask`, `CallerResolver` LRU cache, `BreakerSet._snapshot`, `PricingTable`.
- **Shared via Redis** (one source of truth across replicas): rate-limit bucket counters, breaker sample counters, half-open probe lock, observation hashes.
- **Shared via Postgres**: caller registry, request audit log.

---

## 3. The hot path in one paragraph

A request hits `/v1/chat/completions`. The `CallerResolver` looks up the bearer token (HMAC-SHA256 with server-side pepper) against the LRU-bounded auth cache, falling back to Postgres on miss. The daily-cap is checked with one SUM query. The `Router` mints a server-side `request_id` (`uuid4.hex`), preserving any caller-supplied `metadata.request_id` in a separate `client_trace_id` column. It then enters a loop: ask the `WeightEngine` (in-process, no I/O) for a candidate; atomically acquire RPM+TPM from the Redis token bucket; if the bucket is dry, exclude this candidate and repick; otherwise call `Vendor.chat()` with a derived per-attempt timeout. On retryable errors (`rate_limited`, `transient_5xx`, `timeout`) the router records the failure to the `Observer`, excludes the candidate, and repicks. On non-retryable caller errors (`bad_request`, `auth`, `content_filtered`) it gives up immediately and returns the right HTTP code. Vendor exception messages are sanitized; the raw `vendor_detail` is retained internally only. On success, an `AttemptRecord` per attempt is enqueued to the `AccountingQueue` (non-blocking) and the response is returned. A background `RefreshTask` ticks once per second to refresh per-candidate weights from Redis aggregates; consecutive failures trigger jittered backoff and increment `REFRESH_ERRORS_TOTAL`.

See [`data-plane.md`](data-plane.md) for the line-by-line walkthrough.

---

## 4. State partitioning

| State | Where it lives | Why |
|---|---|---|
| Caller registry (name → key_hash, daily_cap, enabled) | Postgres `callers` table | Stable, low-cardinality; relational queries OK. |
| Per-attempt audit log | Postgres `requests` table (now includes `client_trace_id`) | Time-series; queried by caller/tier for `/v1/usage` and billing. |
| Rate-limit bucket counters | Redis hash per `(provider, model)` | Fleet-wide cap enforced *atomically* via Lua across replicas. |
| Circuit-breaker sample counters | Redis hash per `(provider, model, epoch_sec)` | Sliding window aggregation across replicas. |
| Half-open probe lock | Redis `SET NX EX` key | Exactly one replica probes the half-open vendor. |
| Observation samples (latency, error rates) | Redis hash per `(provider, model, epoch_sec)` | Same shape as breakers; consumed by `RefreshTask`. |
| Breaker state snapshot | In-process dict on each replica (rebuilt atomically) | Hot-path read; refreshed by `RefreshTask`. |
| Routing weights | In-process dict on each replica | Hot-path read; replaced atomically by `RefreshTask`. |
| Caller auth lookup | In-process LRU on each replica (60s TTL, bounded size) | Avoids per-request DB hit; the typical caller has a fixed key for years. |
| Pricing table | Vendored LiteLLM JSON in `gateway/data/` → in-memory `PricingTable` | Read once at boot, immutable thereafter; consumed per-attempt by the router. |
| Outbound vendor API keys | `SecretsManager` (env in prod, in-memory in dev) | Single boundary; vendor adapters never touch `os.environ`. |

The system's correctness relies on the **boundaries** here: anything labelled "Redis" is the single source of truth across replicas; anything labelled "in-process" is a cache that must be safely reconstructible from Redis/Postgres (or, for the pricing table, from the shipped JSON).

---

## 5. The routing decision

Every refresh tick (~1 Hz) the gateway recomputes, for each candidate in every tier:

```
health_score  = (1 - error_rate) × (target_latency / (target_latency + mean_latency))   ∈ [0, 1]
budget_score  = min(rpm_remaining / rpm_cap, tpm_remaining / tpm_cap)                   ∈ [0, 1]
effective_w   = base_weight × health_score × budget_score    (0 if breaker.OPEN or w < floor)
```

`Router.route` then asks `WeightEngine.pick(...)` which performs **weighted-random selection** over the non-excluded, non-zero-weight candidates. The same RNG (`random.SystemRandom`, or a seeded one in tests) is used per-replica. The `min_weight_floor` (default 0.02) prevents candidates with ~0 weight from getting an occasional unlucky pick.

This decision is read from an in-process cache — `pick()` never touches Redis or Postgres. The cache is replaced *atomically* (rebuild new dict, swap reference) by the refresh task, so a routing decision is consistent with a recent snapshot but never blocks on I/O.

---

## 6. Failover & deadline

`Router.route` enforces three nested time budgets:

- **`total_budget_s` (10s default)** — the deadline for the whole request, regardless of how many candidates are tried.
- **`per_attempt_max_s` (8s default)** — upper bound on any single vendor call.
- **`deadline_buffer_s` (0.5s)** — slack reserved before the deadline so the response can be marshalled.

The per-attempt timeout is computed as `min(max(0.1, remaining - buffer), per_attempt_max_s)` — i.e., shorter as the deadline approaches.

Retryable errors (`rate_limited`, `transient_5xx`, `timeout`) cause the failed candidate to be added to an `exclude` set and the loop continues. Non-retryable errors (`bad_request`, `auth`, `content_filtered`) propagate immediately as caller errors.

When the deadline drains mid-failover, the router returns `RouterErrorKind.DEADLINE_EXCEEDED` → HTTP 504. When all candidates are excluded with budget remaining, `UPSTREAM_UNAVAILABLE` → HTTP 503.

Input validation now bounds request size before any of this runs: `max_tokens ≤ 16384`, messages ≤ 512, `Message.content ≤ 200k`, aggregate payload ≤ 1M, metadata constraints, `CallerEntry.name` regex. See [`modules/router.md`](modules/router.md).

---

## 7. Resilience design

| Failure | What happens |
|---|---|
| One vendor returns 5xx storms | Per-attempt failures fill breaker samples → ratio crosses 30% with ≥20 samples → breaker OPENs → weight collapses to 0 for `open_duration_s` (30s) → candidate dropped from selection. After 30s, breaker → HALF_OPEN; one replica wins the probe lock and tries; result flips it back to CLOSED or OPEN. The `BreakerSet` snapshot is rebuilt atomically every tick. |
| One vendor rate-limits us | Bucket goes dry → `bucket_score` collapses → weight collapses → router skips this candidate on next pick without even trying. |
| Vendor slows down | `mean_latency_s` rises → `health_score` decays via `target / (target + observed)` → weight shrinks proportionally, but the candidate is still pickable. |
| Redis is unreachable | `RedisTokenBucket.try_acquire`, `BreakerSet.refresh_snapshot`, `Observer.record_*` all raise. `Router.route` does not handle these; they propagate as 500s. `RefreshTask` now applies jittered backoff on consecutive failures and emits `REFRESH_ERRORS_TOTAL`. The `REDIS_DOWN` gauge is still set only at boot and is not flipped on per-call failures — open gap; see `cr-1.md` §6.2. |
| Postgres is unreachable at boot | `Database.connect()` raises → uvicorn fails to start lifespan. In real mode, `GATEWAY_DB_DSN` is required (no implicit dev default). |
| Postgres is unreachable mid-flight | `AccountingQueue._flush` exceptions are caught and counted in `ACCOUNTING_DROPPED` live (not just at shutdown); the request itself still succeeds (write-behind absorbs the outage). |
| Accounting queue fills | Bounded deque drops the oldest record and live-emits `ACCOUNTING_DROPPED`. cr-1 §6.3 closed. |
| Caller's auth cache TTL is in effect during a key rotation / disable | The change takes effect within 60s. There is no eager invalidation. The cache is LRU-bounded so unbounded growth (cr-1 §3.4) is closed; the 60s revocation window remains open. |

See [`modules/router.md`](modules/router.md), [`modules/accounting.md`](modules/accounting.md), [`modules/auth.md`](modules/auth.md), [`modules/routing.md`](modules/routing.md).

---

## 8. Configuration & deployment topology

- **Single Pydantic `Config` object** built from `config.yaml` at boot, validated, and held in a `ConfigHolder`. SIGHUP rereads the same file and atomically swaps the holder's value. `gateway/config.py` is now a package (`gateway/config/`).
- **Env-var overrides** for `provider_mode` and `secrets_mode` so the same image runs in mock-mode dev and real-mode prod with one config file.
- **Real-mode hard requirements:** `GATEWAY_DB_DSN` must be set explicitly (no implicit default). Boot logs a warning if the Postgres DSN doesn't carry `sslmode=require`/`verify` or if `GATEWAY_REDIS_URL` is not `rediss://`.
- **`/metrics` is auth-gated** by `GATEWAY_METRICS_TOKEN` (read from the secrets manager, constant-time compared). Fail-closed if the token is unset.
- **`/readyz` returns only `{"status":"ready"}`** — no tier or provider names leaked (cr-1 §8.3 fix).
- **Two real I/O endpoints needed:** Redis (any version ≥ 6 for scripting + `SET NX EX`), Postgres (≥ 14 for `BIGSERIAL`/`TIMESTAMPTZ` semantics, but anything ≥ 10 works).
- **Stateless replicas** — any replica handles any request. The only in-process state is caches (auth, breaker snapshot, routing weights, pricing) and queues (accounting), all reconstructible from Redis + Postgres + shipped JSON.
- **Vendor SDK choices:** `openai`, `anthropic`, `google-genai` — all pinned. Each adapter normalizes vendor exceptions into the `ProviderError` taxonomy and sanitizes outbound messages. Missing API keys in real mode are tolerated (the vendor is silently skipped) so an operator can run with only the keys they have.
- **Container runs as non-root** (uid 10001) per the Dockerfile.

### Pre-deploy operations

App boot no longer runs migrations or caller seeding. Operators must, in order:

1. `python -m scripts.apply_migrations` — applies SQL under `migrations/` using a `pg_advisory_xact_lock`; fails loud if the directory is missing.
2. `python -m scripts.seed_callers` — seeds callers from config; boot-time seeding is otherwise gated by `GATEWAY_SEED_CALLERS=1` (cr-1 §2.2 fix). The app only *warns* when `callers` is empty.
3. Start the app (`uvicorn gateway.app:app` or the container entrypoint).

`scripts/setup.sh` orchestrates 1 and 2. See [`modules/scripts.md`](modules/scripts.md), [`modules/app.md`](modules/app.md), [`modules/config.md`](modules/config.md).

---

## 9. Observability

The gateway emits three streams:

1. **Prometheus metrics** at `/metrics` (auth-gated) — request count by `(caller, tier, outcome)`, end-to-end latency histogram, per-attempt outcome by `(provider, model, status)`, current routing weight per candidate, current breaker state, current bucket remaining, USD spent per `(caller, tier, provider)`, `ACCOUNTING_DROPPED`, `REFRESH_ERRORS_TOTAL`, `REDIS_DOWN`.
2. **Structured logs** via `structlog` — JSON to stdout, one line per event, ISO timestamps, log levels honored from `GATEWAY_LOG_LEVEL`. A redaction processor reads its rules from `gateway/config/logger.json` so secrets and vendor detail never reach stdout (cr-1 §4.2 fix). Defer to [`modules/observability.md`](modules/observability.md) for the rule schema.
3. **Postgres audit log** — `requests` table contains every attempt (not just the successful one), with `caller`, `tier`, `provider`, `model`, `attempt_idx`, token counts, USD cost (via `pricing.py`), latency, status, the vendor's request id when available, and `client_trace_id` (the caller-supplied trace, kept separate from the server-minted `request_id`).

Operators query Postgres for billing reconciliation and scrape `/metrics` for dashboards.

See [`modules/observability.md`](modules/observability.md), [`modules/pricing.md`](modules/pricing.md).

---

## 10. Cross-references

- Request walkthrough: [`data-plane.md`](data-plane.md)
- Per-module deep dives: [`modules/`](modules/) — including [`modules/pricing.md`](modules/pricing.md) and [`modules/scripts.md`](modules/scripts.md) for the newest additions.
- Known issues: [`../code-review/cr-1.md`](../code-review/cr-1.md), [`../code-review/t-1.md`](../code-review/t-1.md)
