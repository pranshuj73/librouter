# Gateway System Documentation

This directory documents the internal architecture of the LLM Gateway as it exists in `gateway/` at `HEAD`. It is intended for engineers operating, extending, or onboarding to the service.

For project-level docs (plan, progress, code reviews) see `../PLAN.md`, `../PROGRESS.md`, and `../code-review/`.

## Where to start

| If you want to… | Read |
|---|---|
| Get the 10-minute big picture | [`architecture.md`](architecture.md) |
| Trace one `/v1/chat/completions` request end-to-end | [`data-plane.md`](data-plane.md) |
| Understand a specific module | the matching file under [`modules/`](modules/) |

## Module index

| File | Module | Owns |
|---|---|---|
| [`modules/app.md`](modules/app.md) | `gateway/app.py` | FastAPI app, lifespan, HTTP endpoints |
| [`modules/config.md`](modules/config.md) | `gateway/config.py`, `gateway/models.py` | YAML config schema, SIGHUP reload, all Pydantic models |
| [`modules/auth.md`](modules/auth.md) | `gateway/auth.py` | Bearer-token → `Caller` resolution with 60s cache |
| [`modules/secrets.md`](modules/secrets.md) | `gateway/secrets.py` | `SecretsManager` ABC + env / mock implementations |
| [`modules/db.md`](modules/db.md) | `gateway/db.py`, `migrations/` | Postgres pool, schema, batched writes, queries |
| [`modules/accounting.md`](modules/accounting.md) | `gateway/accounting.py` | Async per-attempt write-behind queue |
| [`modules/redis-state.md`](modules/redis-state.md) | `gateway/redis_state.py` | Redis async client + Lua scripts |
| [`modules/ratelimit.md`](modules/ratelimit.md) | `gateway/ratelimit.py` | Two-dimensional token bucket per candidate |
| [`modules/breaker.md`](modules/breaker.md) | `gateway/breaker.py` | Sliding-window circuit breaker with half-open probe |
| [`modules/router.md`](modules/router.md) | `gateway/router.py` | Adaptive failover router (the hot path) |
| [`modules/routing.md`](modules/routing.md) | `gateway/routing/` | `WeightEngine`, `Observer`, `RefreshTask` |
| [`modules/pricing.md`](modules/pricing.md) | `gateway/pricing.py` | USD cost computation via vendored LiteLLM JSON |
| [`modules/providers.md`](modules/providers.md) | `gateway/providers/` | `Vendor` ABC, real adapters, mock adapters |
| [`modules/observability.md`](modules/observability.md) | `gateway/logging.py`, `gateway/metrics.py`, `gateway/errors.py` | structlog config, Prometheus collectors, error taxonomy |
| [`modules/scripts.md`](modules/scripts.md) | `scripts/` | Pre-deploy migration + caller-seeding tools |

## Conventions used in these docs

- **File:line refs** point at `HEAD`. They are stable enough to navigate from but will drift over time.
- Code snippets are illustrative, not authoritative — always check the source.
- ASCII diagrams use `→` for synchronous calls, `⇢` for fire-and-forget enqueues, and `┄` for background loops.
- Each module doc has the same skeleton: **Purpose · Public surface · Internals · Concurrency model · Failure modes · Configuration knobs · Open questions / known gaps**.

## Related reading

- [`../code-review/cr-1.md`](../code-review/cr-1.md) — code review #1 (security, infra, auth, validation gaps)
- [`../code-review/t-1.md`](../code-review/t-1.md) — test review #1
- [`../PLAN.md`](../PLAN.md) — original implementation plan
