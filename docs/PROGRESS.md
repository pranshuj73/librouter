# LLM Gateway — Implementation Progress

Source of truth for what's done and what's next. Pair with `docs/PLAN.md`.

## TDD ladder

A step is **green** only when its test file passes locally.

| # | Module | Tests | Impl | Notes |
|---|---|---|---|---|
| 0 | Project skeleton (deps, dirs, Dockerfile, docker-compose, configs) | n/a | done | requirements.txt, pyproject.toml, Dockerfile, docker-compose.yml, config.yaml, config.dev.yaml |
| 1 | `gateway/models.py` | 23 green | done | |
| 2 | `gateway/secrets.py` | 10 green | done | |
| 3 | `gateway/redis_state.py` | 9 green | done | EVALSHA + NOSCRIPT fallback to EVAL; probe lock uses plain SET NX EX |
| 4 | `gateway/ratelimit.py` | 6 green | done | |
| 5 | `gateway/breaker.py` | 8 green | done | half-open transition decided by first post-transition sample |
| 6 | `gateway/routing/observe.py` | 5 green | done | per-second hash buckets `gw:obs:{p}:{m}:{epoch}` |
| 7 | `gateway/routing/weights.py` | 15 green | done | distribution gate confirmed in step 14 |
| 8 | `gateway/routing/refresh.py` | 4 green | done | |
| 9 | `gateway/providers/base.py` + `providers/mock/*` | 10 green | done | scripted responses, latency/error injection |
| 10 | `gateway/router.py` | 8 green | done | adaptive failover, deadline math, 400/503/504 paths |
| 11 | `gateway/accounting.py` | 5 green | done | drop-oldest on overflow |
| 12 | `gateway/db.py` + `migrations/0001_init.sql` | 5 green | done | testcontainers Postgres |
| 13 | `gateway/auth.py` | 9 green | done | 60s positive+negative cache |
| 14 | `gateway/app.py` + e2e | 7 green | done | weighted-distribution gate 50/30/20 ±10% over 400 reqs |
| 15 | Real vendor adapters | 36 green | done | SDK-level monkeypatched contract tests for openai/anthropic/google |
| 16 | `gateway/metrics.py` + `gateway/logging.py` | n/a | done | wired in app.py; verified by e2e `/metrics` scrape |

**Totals:** 136 unit tests + 7 e2e + 5 db = 148 tests, all green.

## Gates

- [x] `pytest` green (148 / 148)
- [x] Weighted-distribution gate: 50/30/20 within ±10% over 400 mock requests (passed in e2e)
- [x] `docker compose up` + curl smoke test (`/healthz`, `/v1/chat/completions`, `/metrics` all return cleanly)
- [x] Volume persistence check: a `requests` row and a Redis canary key both survived `docker compose down && docker compose up`
- [x] `gateway_routing_weight` series at runtime matches configured base weights (50/30/20 fast tier, 40/40/20 smart tier)

## Deviations from PLAN

- **fakeredis-with-lua used for unit tests** instead of testcontainers Redis. testcontainers Postgres+Redis are still used in `test_db.py` and `test_app_e2e.py` so we still exercise real Redis semantics end-to-end. Trade-off: 100x faster unit feedback loop; mismatch risk caught by e2e.
- **`respx` not used for vendor adapter contract tests** — respx didn't intercept the SDK's internal httpx client cleanly. Switched to SDK-level `monkeypatch.setattr` on `<client>.chat.completions.create` / `<client>.messages.create` / `<client>.aio.models.generate_content`. Same goal (verify ProviderError mapping + ChatResult shape), simpler implementation.
- **Single shared mock vendor base** (`providers/mock/_base_mock.py`) — three mocks differ only by `name` and `_vrid_prefix`, so the implementation lives in one file with subclasses for each vendor.
- **Probe lock uses `SET NX EX` directly**, not Lua, because the operation is already atomic in Redis and avoids a script-cache pitfall in fakeredis under concurrency.

## Open follow-ups

- Wire a pub/sub subscriber on `gw:brk-events` so breaker transitions converge within <100ms across replicas (PLAN section "Circuit breakers"). Today, replicas reconcile within the 1s refresh tick.
- Add a structured `request_id` middleware so every log line includes it (currently the router uses metadata.request_id when supplied; auto-generated IDs would be nicer).
- Real-vendor SDKs are wired but real network smoke tests are gated on actual API keys; not included in CI.
