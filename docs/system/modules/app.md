# gateway/app.py — FastAPI app, lifespan, HTTP endpoints

## Purpose

`gateway/app.py` is the only module that talks to FastAPI. It owns the ASGI app object, the lifespan context manager that wires every long-lived collaborator (router, weight engine, refresh task, accounting queue, Postgres pool, Redis client), and the four HTTP endpoints the gateway exposes. Everything below it is plain async Python — no web-framework imports leak past this file.

It exists to be the *boot orderer* and the *HTTP boundary*. Boot order matters because the router is unsafe to use before the weight engine has been primed (the cache would be empty and every `pick()` would return `None`); the lifespan runs an initial `RefreshTask.tick()` synchronously to fill it before yielding. The HTTP boundary is the only place where typed Python exceptions get turned into HTTP status codes — see `_http_status_for` and the validation rules embedded in `ChatCompletionRequest`.

## Public surface

| Symbol | Type | Purpose |
|---|---|---|
| `app` | `FastAPI` | The ASGI app. Importable as `gateway.app:app` by uvicorn. |
| `lifespan(app)` | async context manager | Boots collaborators, populates `app.state`, tears down on shutdown. |
| `chat_completions(request, body, authorization)` | `POST /v1/chat/completions` handler | The hot path. Returns `ChatCompletionResponse`. |
| `v1_usage(request, authorization, caller=None)` | `GET /v1/usage` handler | Per-caller usage summary. The `caller` query param is intentionally ignored (#3.2). |
| `metrics(request, authorization)` | `GET /metrics` handler | Bearer-gated Prometheus exposition (`GATEWAY_METRICS_TOKEN`). |
| `healthz()` | `GET /healthz` handler | Cheap liveness probe. Always returns `{"status": "ok"}`. |
| `readyz(request)` | `GET /readyz` handler | Returns `{"status": "ready"}` only — no tier names disclosed (cr-1 §8.3, commit `84e5e64`). |
| `_resolve_caller(request, authorization) -> Caller` | helper | Bearer → `Caller`; raises `HTTPException(401)` on failure. |
| `_http_status_for(kind: RouterErrorKind) -> int` | helper | Maps the four `RouterErrorKind` values to HTTP statuses. |
| `_refresh_observability_gauges(request)` | helper | Walks the tier config and writes per-candidate gauges before each `/metrics` scrape. |
| `_apply_env_overrides(cfg, *, provider_mode, secrets_mode)` | helper | Re-validates `Config` with env-var overrides, raising `ValidationError` on bad input. |
| `_METRICS_TOKEN_KEY` | module constant | `"GATEWAY_METRICS_TOKEN"` — the `SecretsManager` key the `/metrics` handler reads. |

Nothing else is imported from this module by anything else in `gateway/`.

## Internals

### Boot sequence (lifespan)

The lifespan at `app.py:90-232` runs once per process. Order matters; the comments below mark dependencies. Migrations and caller seeding are **no longer** run inside lifespan — ops invokes `scripts/apply_migrations.py` and `scripts/seed_callers.py` (orchestrated by `scripts/setup.sh`) before booting the app (commit `40eb4f6`).

1. Logging + config:

   ```python
   configure_logging(os.environ.get("GATEWAY_LOG_LEVEL", "INFO"))         # app.py:92
   cfg = load_config(config_path)                                          # app.py:95
   cfg = _apply_env_overrides(cfg, provider_mode=..., secrets_mode=...)    # app.py:96 — #9.2
   holder = ConfigHolder(cfg, source_path=config_path)                     # app.py:101
   install_sighup_reload(holder)                                           # app.py:102
   ```

   `_apply_env_overrides` re-runs `Config.model_validate` so an invalid `GATEWAY_PROVIDER_MODE` (e.g. `reel`) trips at boot rather than deep inside `build_vendors`.

2. DSN guard (commit `8e046e5`): in `provider_mode == "real"` a missing `GATEWAY_DB_DSN` raises `RuntimeError` at `app.py:110-114`. In mock mode the dev default `postgres://gateway:gateway@localhost:5432/gateway` is used.

3. TLS warnings (commit `196bf73`, `app.py:122-132`): in real mode, log a warning if the Postgres DSN does not contain `sslmode=require`/`sslmode=verify` and if `GATEWAY_REDIS_URL` does not start with `rediss://`. Warnings, not failures — operators behind a TLS-terminating sidecar can opt out.

4. Redis client + Lua scripts:

   ```python
   r = redis_async.from_url(redis_url, decode_responses=False)             # app.py:134
   state = RedisState(r); await state.load_scripts(); REDIS_DOWN.set(0)    # app.py:135-137
   ```

   A bad Redis URL fails the lifespan before serving any requests. `REDIS_DOWN.set(0)` is the only update to that gauge; nothing in the running app flips it to `1` — cr-1 §6.2 open gap.

5. Database — connect only (commit `40eb4f6`):

   ```python
   db = Database(dsn=db_dsn); await db.connect()                           # app.py:139-140
   ```

   `db.connect()` opens the asyncpg pool. No migrations run here. A non-fatal heads-up is logged at `app.py:142-149` if the `callers` table is empty.

6. Secrets manager (`app.py:151`). In `secrets_mode == "mock"` the manager is seeded from env vars for `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `GATEWAY_METRICS_TOKEN`, and `GATEWAY_KEY_HASH_PEPPER` (`app.py:156-169`). In real mode those names are resolved against the configured backend on demand.

7. Pepper resolution (commit `4bcccd4`): `secrets.get("GATEWAY_KEY_HASH_PEPPER")` at `app.py:171` is fail-loud — a missing pepper raises and aborts startup. It's later passed to `CallerResolver` for HMAC-SHA256 caller-key hashing.

8. Caller seeding is **not** run at boot. The previous behaviour (config-driven seeding) is gated behind `GATEWAY_SEED_CALLERS=1` (commit `0c569b2`); in the current code path even that branch is intentionally dormant — seeding is `scripts/seed_callers.py`'s job.

9. Vendor build (`app.py:173`). Non-fatal on missing keys; the returned `vendors` dict only contains adapters whose keys resolved. The set of provider names is fed into `RefreshTask(..., available_providers=set(vendors.keys()))` so candidates without a constructible vendor are zeroed in the weight cache.

10. Routing collaborators (`app.py:175-188`):

    ```python
    bucket   = RedisTokenBucket(state=state, limits=cfg.rate_limits)
    breakers = BreakerSet(state=state)
    observer = Observer(state=state, window_s=cfg.routing.health_window_s)
    engine   = WeightEngine(routing=cfg.routing)
    refresh  = RefreshTask(config=cfg, observer=observer, bucket=bucket,
                           breakers=breakers, engine=engine,
                           available_providers=set(vendors.keys()))
    await refresh.tick()    # populate engine cache before yielding
    refresh.start()
    ```

    Without the initial `tick()`, the first ~1s of traffic after boot would 503 (every candidate weight 0).

11. RNG (`app.py:190-195`): seeded `random.Random` if the env var named by `cfg.routing.rng_seed_env` is set; otherwise `random.SystemRandom()`.

12. Accounting queue (`app.py:197-198`): `AccountingQueue(writer=db); await accounting.start()` boots the flush loop.

13. Router + auth (`app.py:200-208`): `Router(...)` and `CallerResolver(db=db, pepper=pepper)`.

14. `app.state` wiring (`app.py:210-219`):

    | Attribute | Used by |
    |---|---|
    | `app.state.cfg` (a `ConfigHolder`) | `/metrics` gauge refresh, future use |
    | `app.state.router` | `/v1/chat/completions` |
    | `app.state.auth` | `_resolve_caller` |
    | `app.state.db` | `/v1/usage`, daily-cap check |
    | `app.state.accounting` | `/v1/chat/completions` enqueue |
    | `app.state.refresh` | shutdown only |
    | `app.state.engine` | `/metrics` |
    | `app.state.breakers`, `app.state.bucket` | reserved for future endpoints |
    | `app.state.secrets` | `/metrics` bearer check, future use |

15. Shutdown (`app.py:223-232`): stop `refresh`, stop `accounting` (drains pending rows; live `ACCOUNTING_DROPPED` already accounts for overflow), `db.close()`, `r.aclose()`.

### `/healthz` and `/readyz`

`/healthz` (`app.py:257-259`) returns `{"status": "ok"}` unconditionally — it does not touch Postgres, Redis, or any in-process state. Cheap liveness only.

`/readyz` (`app.py:262-266`) returns only `{"status": "ready"}` once the lifespan has yielded. The previous body included `tiers` (the configured tier names); that field was removed in commit `84e5e64` (cr-1 §8.3) because the endpoint is unauthenticated and tier names are configuration data. Operators wanting candidate-level readiness should scrape `/metrics`.

### `/v1/chat/completions` (the only request-serving endpoint)

The handler at `app.py:357-411` is intentionally short. The response `id` is the gateway-generated `uuid4().hex` set inside `router.route()`; the caller's `metadata.request_id` (if any) is stored separately on every `AttemptRecord` as `client_trace_id` (commit `e74a3f3`). Sequence:

```python
caller = await _resolve_caller(request, authorization)          # 401 on bad bearer
if caller.daily_token_cap is not None:                          # 429 if cap exhausted
    used = await db.caller_tokens_used_today(caller.name)
    if used >= caller.daily_token_cap: raise HTTPException(429, ...)

t0 = time.monotonic()
try:
    result = await router.route(body, caller)                   # the hot loop
except RouterError as e:
    REQUESTS_TOTAL.labels(caller, body.model, outcome=e.kind.value).inc()
    REQUEST_LATENCY.labels(body.model, e.kind.value).observe(elapsed)
    raise HTTPException(status_code=_http_status_for(e.kind), detail=e.body.model_dump())

REQUESTS_TOTAL.labels(caller, body.model, outcome="ok").inc()
REQUEST_LATENCY.labels(body.model, "ok").observe(elapsed)
for a in result.attempts:                                       # one per attempt, success+fail
    ATTEMPTS_TOTAL.labels(a.provider, a.model, a.status).inc()
    if a.status == "ok":
        COST_USD_TOTAL.labels(caller, body.model, a.provider).inc(a.cost_usd)
    accounting.enqueue(a)
return result.response
```

See [`../data-plane.md`](../data-plane.md) for the line-by-line walkthrough and [`router.md`](router.md) for what `router.route` does.

### `/metrics` auth

The handler at `app.py:269-294` is bearer-gated. The token is read from `SecretsManager.get("GATEWAY_METRICS_TOKEN")` (the key constant is `_METRICS_TOKEN_KEY` at `app.py:66`), populated either by the env-var fallback in mock mode (`app.py:156-169`) or by the real secrets backend in prod. The handler:

```python
if not sm.has(_METRICS_TOKEN_KEY): return _unauth          # fail-closed
expected = sm.get(_METRICS_TOKEN_KEY)
provided = authorization[len("Bearer "):].strip() if authorization and authorization.startswith("Bearer ") else ""
if not hmac.compare_digest(expected, provided): return _unauth
```
`app.py:283-290`

Comparison uses `hmac.compare_digest` to avoid a timing oracle. If the token is unset the endpoint fails closed — operators cannot accidentally expose `/metrics` by forgetting to configure auth (cr-1 §3.3, commit `8e046e5`).

`_refresh_observability_gauges` (`app.py:297-339`) walks `cfg.tiers` and calls `WeightEngine.signals_for(ref)` for each candidate, recomputing `health_score / budget_score / effective_weight` on the spot and writing to `ROUTING_WEIGHT`, `BREAKER_STATE`, `BUCKET_REMAINING`. The signals themselves are the snapshot the `RefreshTask` last wrote — see [`routing.md`](routing.md).

### `/v1/usage`

`v1_usage` at `app.py:342-354` is bearer-gated and **caller-scoped**: it always queries the authenticated caller's summary, regardless of the `caller=` query param. The signature keeps `caller: str | None = None` for API compatibility but the handler comment explicitly marks it `# noqa: ARG001 — ignored; kept for API compat` (cr-1 §3.2, commit `8e046e5`). Admin-scope querying (a caller asking about another caller) would require an `is_admin` column on `callers` and is explicitly future work.

## Concurrency model

- **One event loop.** All endpoint handlers and the background tasks share uvicorn's default loop. `RefreshTask` and `AccountingQueue` each own one `asyncio.Task`.
- **In-process singletons (per replica):** `Router`, `WeightEngine`, `RefreshTask`, `AccountingQueue`, `CallerResolver` (with its 60s cache), `RedisTokenBucket`, `BreakerSet`. All are constructed once in the lifespan and never replaced.
- **Mutable in-process state:**
  - `WeightEngine._cache` is replaced (not mutated) by `RefreshTask` once per `routing.refresh_interval_ms`. The swap is a single Python assignment, atomic at the interpreter level — readers in `pick()` see either the old complete map or the new one.
  - `BreakerSet._snapshot` follows the same rebuild-then-swap discipline (see `breaker.py:96-158` and `cr-1.md` §6.1).
  - `CallerResolver`'s cache is a `dict` mutated in-place under a single-flight per-token lock; reads outside the locked path may transiently see a stale entry, which is fine — the TTL bound applies.
  - `app.state` attributes are set once in the lifespan and never reassigned.
- **No mutex / no `asyncio.Lock` is used in `app.py` itself.** The only synchronization primitive in the request path is the per-token lock inside `CallerResolver`.
- **Postgres** access happens through `asyncpg`'s connection pool (per-replica); pool size is a `Database`-level concern.
- **Redis** access uses one `redis.asyncio` client shared across all collaborators. The client multiplexes commands; pipelines used by `BreakerSet` and `Observer` are not transactional (`transaction=False`).

## Failure modes

| What goes wrong | What the caller sees | What gets retried | What gets dropped |
|---|---|---|---|
| Pydantic rejects the body | 422 (FastAPI default) | nothing | the request |
| Bearer missing / unknown / disabled caller | 401 `{"type":"auth"}` | nothing | the request |
| Daily token cap reached | 429 `{"type":"caller_rate_limit","retryable":false}` | nothing | the request |
| Router exhausts all candidates | 503 `{"type":"upstream_unavailable","retryable":true}` | candidates were retried in the loop | the request |
| Deadline drains mid-failover | 504 `{"type":"deadline_exceeded","retryable":true}` | candidates were retried in the loop | the request |
| Vendor returns `BadRequest` / `ContentFiltered` | 400 with canonical message | nothing | the request |
| Vendor returns `AuthError` (our key rejected) | 401 with canonical message | nothing | the request |
| Redis unreachable mid-request | 500 (uncaught) | nothing | the request; `REDIS_DOWN` is *not* flipped (open gap, `cr-1.md` §6.2) |
| Postgres unreachable at boot | uvicorn fails to start | n/a | n/a |
| Postgres unreachable for the cap query (mid-request) | 500 (uncaught) | nothing | the request |
| Postgres unreachable for the accounting flush | 200 to the caller; rows dropped | nothing | rows in the queue past capacity; `ACCOUNTING_DROPPED` increments live |
| Accounting queue full at enqueue time | 200 to the caller; oldest record dropped | nothing | the dropped record; `ACCOUNTING_DROPPED` increments |
| `/metrics` token unset | 401 from the scrape | n/a | the scrape |

Error taxonomy lives in `gateway/errors.py`. `_http_status_for` (`app.py:414-420`) only handles the four `RouterErrorKind` values; everything else (e.g. caller-error mapping for `BadRequest`) is handled inside the router via `caller_error_for` and surfaces as `RouterError(INVALID_REQUEST)` or `RouterError(AUTH)` before reaching the handler.

## Configuration knobs

App-level knobs (env vars read in the lifespan) and the relevant `Config` fields:

| Knob | Type | Default | Read from | Controls |
|---|---|---|---|---|
| `GATEWAY_LOG_LEVEL` | str | `INFO` | env | structlog level (`app.py:92`) |
| `GATEWAY_CONFIG` | path | `config.yaml` | env | YAML config file path (`app.py:94`) |
| `GATEWAY_PROVIDER_MODE` | `mock` \| `real` | (from yaml) | env, overrides yaml | which adapter set `build_vendors` builds (`app.py:98`) |
| `GATEWAY_SECRETS_MODE` | `mock` \| `env` | (from yaml) | env, overrides yaml | which `SecretsManager` to build (`app.py:99`) |
| `GATEWAY_REDIS_URL` | url | `redis://localhost:6379/0` | env | Redis client target (`app.py:104`); in real mode, a non-`rediss://` value triggers a TLS warning (`app.py:128-132`) |
| `GATEWAY_DB_DSN` | dsn | required in real mode (`app.py:110-114`); falls back to `postgres://gateway:gateway@localhost:5432/gateway` in mock mode | env | Postgres pool target (`app.py:109,139`); in real mode, missing `sslmode=require` triggers a TLS warning (`app.py:122-127`) |
| `GATEWAY_SEED_CALLERS` | `0`/`1` | `0` | env | Re-enable boot-time caller seeding from `config.callers`. In current code path even this is dormant — seeding lives in `scripts/seed_callers.py` (commit `0c569b2`). |
| `GATEWAY_METRICS_TOKEN` | str | unset → `/metrics` returns 401 | env (mock mode, seeded into `SecretsManager` at `app.py:160-169`) or real secrets backend | bearer token for `/metrics` (`app.py:283-290`) |
| `GATEWAY_KEY_HASH_PEPPER` | str | required (fail-loud at boot, `app.py:171`) | env (mock) or real secrets backend | HMAC-SHA256 pepper for caller-key hashing in `CallerResolver` (commit `4bcccd4`) |
| `routing.rng_seed_env` | str \| None | None | yaml `routing.rng_seed_env` | which env var (if set) seeds the routing RNG; otherwise `SystemRandom` |
| `tiers` | dict | required | yaml | tier → candidate list |
| `prices`, `rate_limits` | dicts | required | yaml | cost calc, bucket caps |

Router-level knobs (`total_budget_s`, `per_attempt_max_s`, `deadline_buffer_s`) are *not* surfaced through config in the current code; they're the constructor defaults at `router.py:104-106`. See [`router.md`](router.md).

Migrations and caller seeding are pre-deploy operations: `scripts/setup.sh` calls `python -m scripts.apply_migrations` and `python -m scripts.seed_callers`. Both honour `GATEWAY_DB_DSN`; the seeder additionally requires `GATEWAY_KEY_HASH_PEPPER` and per-caller `GATEWAY_SEED_KEY_<NAME>` env vars (see `scripts/seed_callers.py`).

## Open questions / known gaps

- `REDIS_DOWN` is set to 0 at boot but never updated when Redis actually goes down. The router does not catch `redis.RedisError`, so a Redis outage surfaces as a string of 500s instead of being signalled to operators (`cr-1.md` §6.2).
- The daily token cap is checked once per request and does not re-check between attempts. A burst near the cap can overshoot by one request's tokens.
- ~~`/v1/usage` IDOR (#3.2)~~ — resolved in commit `8e046e5`; the `?caller=` param is now ignored and the handler always uses the authenticated caller.
- ~~`/readyz` discloses tier names (#8.3)~~ — resolved in commit `84e5e64`; the body is `{"status": "ready"}` only.
- ~~`/metrics` unauthenticated (#3.3)~~ — resolved in commit `8e046e5`; bearer-gated via `GATEWAY_METRICS_TOKEN` from `SecretsManager`, fail-closed, constant-time compare.
- There is still no readiness gating: `/readyz` reports ready as long as the lifespan has yielded — it does not check Redis or Postgres liveness. A degraded backplane is visible only via the 5xx rate.
- No request-id middleware. The router generates its own `uuid4().hex` per request (`router.py:150`) and it lands in `ChatCompletionResponse.id`, but it isn't echoed in a response header; operators correlate via `requests.request_id`, the new `requests.client_trace_id` (migration `0002_client_trace_id.sql`), and the per-attempt `vendor_request_id` field.
- TLS posture for Postgres/Redis in real mode is only a warning, not an enforced fail-stop (commit `196bf73`, `app.py:122-132`). A misconfigured prod deploy logs but starts.
