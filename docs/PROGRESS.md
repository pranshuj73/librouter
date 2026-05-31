# LLM Gateway — Implementation Progress

Source of truth for what's done and what's next. Pair with `docs/PLAN.md`.

## TDD ladder

Steps below correspond to the ordered TDD ladder in `docs/PLAN.md` § Implementation order. A step is **green** only when its test file passes locally.

| # | Module | Tests | Impl | Notes |
|---|---|---|---|---|
| 0 | Project skeleton (deps, dirs, Dockerfile, docker-compose, configs) | n/a | done | requirements.txt, pyproject.toml, Dockerfile, docker-compose.yml, config.yaml, config.dev.yaml |
| 1 | `gateway/models.py` | pending | pending | |
| 2 | `gateway/secrets.py` | pending | pending | |
| 3 | `gateway/redis_state.py` | pending | pending | needs testcontainers Redis |
| 4 | `gateway/ratelimit.py` | pending | pending | |
| 5 | `gateway/breaker.py` | pending | pending | |
| 6 | `gateway/routing/observe.py` | pending | pending | |
| 7 | `gateway/routing/weights.py` | pending | pending | |
| 8 | `gateway/routing/refresh.py` | pending | pending | |
| 9 | `gateway/providers/base.py` + `providers/mock/*` | pending | pending | |
| 10 | `gateway/router.py` | pending | pending | |
| 11 | `gateway/accounting.py` | pending | pending | |
| 12 | `gateway/db.py` + `migrations/0001_init.sql` | pending | pending | needs testcontainers Postgres |
| 13 | `gateway/auth.py` | pending | pending | |
| 14 | `gateway/app.py` + e2e | pending | pending | |
| 15 | Real vendor adapters (`providers/openai.py`, …) | pending | pending | last — not on v1 critical path |
| 16 | `gateway/metrics.py` + `gateway/logging.py` finalization | pending | pending | written incrementally |

## Gates

- [ ] `pytest` green
- [ ] Coverage ≥ 85% on router/weights/refresh/breaker/ratelimit/accounting
- [ ] `docker compose up` + curl smoke test
- [ ] Weighted-distribution gate: 50/30/20 within ±10% over 1000 mock requests
- [ ] Volume persistence check: PG and Redis state survives `compose down/up`

## Decisions / deviations from PLAN

(none yet)

## Open follow-ups

- (none yet)
