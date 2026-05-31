# Code Review #1 — Internal LLM Gateway

- **Date:** 2026-05-31
- **Reviewer:** Claude (automated review)
- **Scope:** entire repository at `HEAD` (branch `main`) — `gateway/`, `migrations/`, `config.yaml`, `config.dev.yaml`, `docker-compose.yml`, `Dockerfile`, `pyproject.toml`
- **Lines reviewed:** ~3,000 LoC of application code (`gateway/`), plus configs, infra, and migrations
- **Severity scale:** Critical / High / Medium / Low / Info

---

## 1. Executive Summary

The gateway has a clean architecture (vendor abstraction, Redis-backed limiters/breakers, Postgres write-behind accounting, weighted autoroute) and the core data-plane logic is conservatively written. **No SQL-injection, command-injection, deserialization-RCE, or path-traversal issues were found.** Pydantic v2 schemas and `asyncpg` parameter binding eliminate most input-handling risk.

However, several **infrastructure-level and authentication-design issues** materially weaken the production posture:

| # | Severity | Issue | Location |
|---|----------|-------|----------|
| 1 | **Critical** | Boot-time caller-table overwrite from config can re-authorize the dev key in prod | `gateway/app.py:99-105` + `config.dev.yaml:43` |
| 2 | **Critical** | Hardcoded Postgres credentials `gateway:gateway`, default DSN baked into app | `docker-compose.yml:9-11`, `gateway/app.py:85-87` |
| 3 | **High** | Plaintext dev API key disclosed in committed config (with hash) | `config.dev.yaml:41-43` |
| 4 | **High** | Caller API keys hashed with bare SHA-256 (no salt/pepper, no KDF) | `gateway/auth.py:18-19` |
| 5 | **High** | `/v1/usage` IDOR — any authenticated caller can read any other caller's usage | `gateway/app.py:255-266` |
| 6 | **High** | `/metrics` endpoint exposes per-caller cost/token data with no auth | `gateway/app.py:200-204` |
| 7 | **Medium** | Unbounded in-process auth negative cache — memory-exhaustion DoS | `gateway/auth.py:44, 62` |
| 8 | **Medium** | Caller-supplied `metadata.request_id` overrides the gateway's request id | `gateway/router.py:146`, `gateway/app.py:269-323` |
| 9 | **Medium** | Vendor error strings flowed verbatim to caller (info leak) | `gateway/errors.py:62-66`, providers |
| 10 | **Medium** | No upper bound on `max_tokens` / message length — amplification & cost DoS | `gateway/models.py:138-154` |
| 11 | **Medium** | Concurrent migration run at boot — no advisory lock | `gateway/db.py:51-58` |
| 12 | **Medium** | Postgres connection has no TLS in any environment | `gateway/app.py:85-87`, `docker-compose.yml` |
| 13 | **Medium** | `assert self._pool is not None` strips under `python -O` | `gateway/db.py:46` |
| 14 | **Low** | Caller-auth cache TTL (60s) extends revocation window | `gateway/auth.py:38` |
| 15 | **Low** | `/healthz`, `/readyz` leak tier names without auth | `gateway/app.py:188-198` |
| 16 | **Low** | YAML `safe_load` with file path from env — reload trust on file ACLs | `gateway/config.py:23-26` |
| 17 | **Low** | Postgres + Redis exposed to host in `docker-compose` | `docker-compose.yml:12, 25` |
| 18 | **Info** | Unused `_DBProtocol`/`hash_api_key` import in `app.py`; `_StatusForError` minor refactor | misc |

Two issues — #1 and #6 — should be addressed **before** any production traffic. The rest can be tracked normally.

---

## 2. Hardcoded Secrets & Sensitive Exposures

### 2.1 Critical: dev key + matching hash committed (`config.dev.yaml:41-43`)

```yaml
# Dev caller. Key: "dev-key-do-not-use-in-prod"
- name: dev
  key_hash: "sha256:ee6817dd35cc5568f1182c9bdaf430580f0b7ce8d44939244c4014bba01f9296"
```

The plaintext token (`dev-key-do-not-use-in-prod`) is in the same repository as its hash. Anyone with read access to the repo (or any old commit) knows a working bearer token for *any* deployment that loads `config.dev.yaml`. Combined with finding 2.2 this is **directly exploitable**.

**Recommendation:** rotate the dev key, do not commit either the plaintext or the hash. Generate dev keys on first boot (e.g. write to `./.dev-secrets/` which is gitignored) and print the value once.

### 2.2 Critical: dev caller can be re-seeded into prod DB (`gateway/app.py:99-105`)

```python
for c in cfg.callers:
    await db.upsert_caller(
        name=c.name, key_hash=c.key_hash,
        daily_token_cap=c.daily_token_cap, enabled=c.enabled,
    )
```

`upsert_caller` (`gateway/db.py:95-112`) does:

```sql
ON CONFLICT (name) DO UPDATE
  SET key_hash        = EXCLUDED.key_hash,
      daily_token_cap = EXCLUDED.daily_token_cap,
      enabled         = EXCLUDED.enabled
```

If a prod replica is started with `GATEWAY_CONFIG=/code/config.dev.yaml` (mistake, copy-paste, accidental env-var leak from staging Helm chart), it will **silently overwrite production caller key hashes** with the dev hash — granting `dev-key-do-not-use-in-prod` access to prod. The comment "dev convenience; in prod use a CLI" acknowledges the risk but does not enforce it.

**Recommendation:**
- Skip seeding when `provider_mode == "real"` and/or `secrets_mode == "env"`.
- Or gate behind a `GATEWAY_SEED_CALLERS=1` env flag, off by default.
- Reject mismatch between config provider_mode and a `GATEWAY_ENV=prod` marker.

### 2.3 Critical: hardcoded DB credentials (`docker-compose.yml`, `gateway/app.py:85-87`)

```yaml
POSTGRES_USER: gateway
POSTGRES_PASSWORD: gateway
```
```python
db_dsn = os.environ.get(
    "GATEWAY_DB_DSN", "postgres://gateway:gateway@localhost:5432/gateway"
)
```

The default DSN works without any env var being set, and ports 5432/6379 are mapped to the host. In prod, if `GATEWAY_DB_DSN` is unset (misconfigured deployment) the app will silently try `gateway:gateway@localhost`, which on a multi-tenant host could connect to an unintended Postgres. There is no fail-loud-if-unset path.

**Recommendation:** require `GATEWAY_DB_DSN` (raise `RuntimeError` if unset and `provider_mode == "real"`); remove the embedded password; mark compose creds as dev-only and avoid host port exposure (or restrict to `127.0.0.1`).

### 2.4 No other hardcoded API keys or secrets

Grep for `password|secret|api_key|credential|token` over `gateway/`, configs, and infra files turned up only the items listed above plus legitimate identifier strings (`OPENAI_API_KEY` as *env var name*, `request_tokens` etc.). Real vendor keys are correctly fetched from `SecretsManager` (`gateway/providers/openai.py:40`, `anthropic.py:49`, `google.py:47`) and never logged.

---

## 3. Authentication & Authorization

### 3.1 High: SHA-256 without salt/pepper (`gateway/auth.py:18-19`)

```python
def hash_api_key(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
```

If the `callers` table is exfiltrated:
- Bare SHA-256 is GPU-trivial to brute-force for any key of < ~12 alphanumeric chars.
- Identical raw keys across environments produce identical hashes (rainbow-table reusable).
- No upgrade path (`sha256:` prefix is the only versioning).

**Recommendation:**
- Adopt a versioned scheme (`v2:hmac-sha256:<hex>` with a server-side pepper read from `SecretsManager`) or `argon2id`.
- Enforce ≥ 32 bytes of CSPRNG entropy at key issuance time.
- Document key rotation (the cache TTL of 60s is currently the rotation latency).

### 3.2 High: `/v1/usage` IDOR (`gateway/app.py:255-266`)

```python
@app.get("/v1/usage")
async def v1_usage(request, authorization, caller: str | None = None):
    await _resolve_caller(request, authorization)
    db = request.app.state.db
    summary = await db.usage_summary(caller=caller)
    return {"items": summary}
```

The authenticated caller is resolved but ignored. Any valid bearer can query `?caller=any-other-caller-name` and read that caller's per-provider token/cost summary. The inline comment acknowledges this: *"an admin caller (out of scope for this revision) would query anyone's"*. This is a confidential-data leak (per-tenant spend).

**Recommendation:** clamp `caller` to `await _resolve_caller(...).name` unless the resolved caller has an `admin` role (add an `is_admin` column to `callers`).

### 3.3 High: `/metrics` unauthenticated (`gateway/app.py:200-204`)

Prometheus metrics labels include `caller`, `tier`, `provider`, and dollar counters (`gateway_cost_usd_total`). Anyone who can reach the gateway port (e.g. via internal mesh or a misconfigured ingress) can scrape per-caller spend.

**Recommendation:**
- Bind `/metrics` to a separate interface (Prometheus side-car only), or
- Require a static `Authorization: Bearer <metrics-token>` header from a secret, or
- Drop the `caller` label from cost counters and expose per-caller spend only via `/v1/usage` (authorized).

### 3.4 Medium: unbounded auth cache (`gateway/auth.py:44, 62`)

```python
self._cache: dict[str, tuple[float, Caller | None]] = {}
...
self._cache[key_hash] = (now, caller)
```

A malicious or buggy client can spam unique bearer values; each gets a negative entry kept until process restart (TTL is only checked on read for matching keys). Memory grows unbounded.

**Recommendation:** swap to `cachetools.TTLCache(maxsize=10_000, ttl=60)` or periodically evict entries with `now - ts > ttl` in a background task.

### 3.5 Medium: `metadata.request_id` is caller-controlled (`gateway/router.py:146`)

```python
request_id = (req.metadata or {}).get("request_id", "")
```

This value is stored in `requests.request_id` for every attempt. Two consequences:
- Caller can spoof another caller's request_id (audit-trail integrity).
- Caller can pass arbitrarily long / control-character text into log/DB rows.

**Recommendation:** treat `metadata.request_id` as a *client-supplied trace id*, store it in a separate column (`client_trace_id`), and assign `request_id = uuid4()` server-side.

### 3.6 Low: caller-auth cache TTL (`gateway/auth.py:38`)

`cache_ttl_s=60.0` means a disabled or rotated key remains valid for up to a minute. Acceptable for normal ops, but document it and consider a Redis pub/sub invalidation path for emergency revocation.

### 3.7 Info: no constant-time comparison

Lookups go to Postgres by `key_hash`, so timing is dominated by the DB roundtrip rather than a byte-by-byte compare; constant-time comparison isn't strictly required. Still, prefer `hmac.compare_digest` if any in-process comparison is ever added.

---

## 4. Input Validation & Untrusted Data

### 4.1 Medium: no upper bound on `max_tokens` or message size (`gateway/models.py:138-154`)

```python
max_tokens: PositiveInt = 1024
messages: list[Message] = Field(min_length=1)
```

A caller can submit `max_tokens=10_000_000` and `messages=[{"role":"user","content":"x"*10_000_000}]`. Effects:
- `estimate_tokens` (`gateway/ratelimit.py:103-109`) returns ~10M, which is silently clamped only by the TPM bucket — but a single such request *empties* the bucket, denying service to others.
- The body is buffered in memory by FastAPI before validation.
- The vendor will reject huge `max_tokens` with a `BadRequest`, which surfaces to the caller verbatim — possible info leak (see 4.3).

**Recommendation:**
- `max_tokens: int = Field(default=1024, gt=0, le=16384)` (or per-tier cap).
- `content: str = Field(max_length=200_000)` on `Message`.
- Add an aggregate `messages` size check in a `model_validator`.
- Configure uvicorn `--limit-concurrency` and `--h11-max-incomplete-event-size`.

### 4.2 Medium: vendor error strings passed through (`gateway/errors.py:59-68`, `gateway/providers/*`)

```python
return 400, ErrorBody(type="invalid_request", message=str(exc), retryable=False)
```

Adapters call `str(e)` on the SDK exception, which often contains the full upstream response body, sometimes including request IDs, internal model names, partial payloads, or even hints of the API key shape. The router then returns this string to the caller.

**Recommendation:** for caller-visible bodies, map to a fixed catalog (`invalid_request`, `auth`, `content_filtered`), and emit the verbose vendor message to structured logs only.

### 4.3 Low: `metadata: dict[str, str]` is unbounded (`gateway/models.py:147`)

A caller can attach a huge metadata blob. Not currently persisted to DB (only `request_id` is read), but Pydantic still validates the entire body.

**Recommendation:** `metadata: dict[str, str] | None = Field(default=None, max_length=16)` and bound each value's length.

### 4.4 Info: `CallerEntry.name` is unrestricted

A misconfigured `config.yaml` can put newlines/control chars into the caller name; it ends up in the `requests.caller` column and Prometheus labels (Prometheus will reject high-cardinality or malformed labels). Add a regex validator like `^[a-z0-9_-]{1,64}$`.

---

## 5. Data Layer

### 5.1 No SQL injection found

All queries use `asyncpg`'s `$N` parameter binding. `usage_summary` (`gateway/db.py:142-168`) builds the `WHERE` clause from a fixed allow-list (`caller`, `since`) and only the placeholder index is interpolated:

```python
clauses.append(f"caller = ${len(args) + 1}")
```

The placeholder number is integer-only and the value goes into `args` for the driver to bind. Safe.

### 5.2 Medium: concurrent boot migrations (`gateway/db.py:51-58`)

```python
async def run_migrations(self) -> None:
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    async with self.pool.acquire() as conn:
        async with conn.transaction():
            for f in files:
                await conn.execute(sql)
```

Several replicas booting simultaneously will all run the same `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`. Idempotent for current migrations, but the moment a non-idempotent statement (e.g. data backfill, `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT ...`) is added, you'll get races and deadlocks.

**Recommendation:** wrap migrations in `pg_advisory_xact_lock(<some constant>)` before running.

### 5.3 Medium: `assert pool is not None` (`gateway/db.py:46`)

```python
@property
def pool(self) -> asyncpg.Pool:
    assert self._pool is not None, "Database.connect() not called"
```

`python -O` strips this and the next access returns `None`, then `await self.pool.acquire()` raises `AttributeError` — confusing failure mode. Use:

```python
if self._pool is None:
    raise RuntimeError("Database.connect() not called")
```

### 5.4 Medium: no TLS to Postgres / Redis

DSN format is `postgres://...` and Redis URL is `redis://...`. In prod the gateway should require `sslmode=require` and `rediss://`. Document and validate this at boot in real mode.

### 5.5 Low: per-row insert vs `COPY`

`write_batch` uses `executemany` (`gateway/db.py:83-93`). Fine at 20 RPS; if traffic grows, switch to `copy_records_to_table` for ~10× throughput.

### 5.6 Low: `requests` table grows unbounded

No retention policy or partitioning. `caller_tokens_used_today` already requires the `caller, ts` index (present), but in a year this table will be huge. Add daily-range partitioning or a TTL job.

---

## 6. Concurrency & Reliability

### 6.1 Medium: snapshot read-modify-write in BreakerSet (`gateway/breaker.py:71`)

`self._snapshot` is mutated from multiple async paths (`refresh_snapshot`, `state`, `_transition_after_window`). asyncio gives cooperative scheduling so there's no preemption inside Python statements, but `refresh_snapshot` does multiple `await` calls while iterating `list(self._snapshot.keys())`. If a snapshot mutation happened on another awaited path, you could miss new entries. Currently no other writer exists at runtime, but the invariant is fragile.

**Recommendation:** introduce a `asyncio.Lock` for snapshot mutation, or rebuild a new dict and atomically swap (`self._snapshot = new`).

### 6.2 Low: `RefreshTask._loop` swallows exceptions broadly (`gateway/routing/refresh.py:99-106`)

Logging without rate-limit can flood if Redis is down. Add jittered backoff and a Prometheus counter for refresh errors. (`REDIS_DOWN` gauge exists but is only set to 0 at boot.)

### 6.3 Low: `AccountingQueue.dropped_total` not exported as a gauge until shutdown (`gateway/app.py:158`)

```python
ACCOUNTING_DROPPED.inc(accounting.dropped_total)
```

This only runs in `lifespan` shutdown — operators won't see live drops. Increment during `_flush` instead.

### 6.4 Low: `random.SystemRandom` allocated per request

`gateway/app.py:128` constructs it once at boot and passes it to the Router — fine. No issue.

---

## 7. Network & Infrastructure

### 7.1 Medium: no HTTPS / no plain-text token warning

Bearer tokens are accepted over plain HTTP (`uvicorn ... --port 8000`). Expected behind a TLS-terminating load balancer, but the gateway does not log a warning when started without one, nor reject `http://`. Add `--proxy-headers` and require `X-Forwarded-Proto: https` in real mode.

### 7.2 Low: Dockerfile runs as root

```dockerfile
FROM python:3.13-slim
... no USER directive ...
CMD ["uvicorn", ...]
```

Add a non-root user:

```dockerfile
RUN useradd -m -u 10001 gateway
USER gateway
```

### 7.3 Low: `docker-compose` exposes 5432/6379 to host

Already noted in 2.3. For dev that's convenient, but should be commented out for any non-local use.

### 7.4 Info: `Dockerfile` does not copy `migrations/`

```dockerfile
COPY ./gateway /code/gateway
COPY ./config.yaml ...
```

`migrations/` is *not* copied into the image. `db.py:23` resolves `_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"` — inside the container that points to `/code/migrations` which won't exist. **This means `run_migrations` is a silent no-op in prod images**, relying entirely on docker-compose's `docker-entrypoint-initdb.d` mount (only on first boot).

This is a functional bug worth flagging here because it affects the auth-table integrity:
- Fresh prod cluster boots → no migration runs → `callers` table missing → `upsert_caller` raises → app crashes loop.
- Or, worse: someone runs the gateway pointed at a Postgres that *does* have the table, but no migrations were applied for v2/v3 schema changes.

**Recommendation:** `COPY ./migrations /code/migrations` in the Dockerfile and validate at boot.

---

## 8. Logging & Observability

### 8.1 Medium: no PII / token redaction in logs

`structlog` is configured with the default `JSONRenderer` (`gateway/logging.py:11-28`). Vendor errors stringified into log lines via `log.exception` can include caller content excerpts. The router does not log the bearer header itself, which is good — but adding any future log around `_resolve_caller` is one mistake away from leaking it.

**Recommendation:** add a `_redact_sensitive` processor that strips `Authorization`, `api-key`, `Bearer ` patterns, and any obvious caller-content fields.

### 8.2 Low: `/metrics` rebuilds gauges by re-importing `weights` inside the handler

`gateway/app.py:217-220` does a function-local import on every scrape. Hoist it to module scope.

### 8.3 Low: tier names visible without auth

`/readyz` returns `list(cfg.tiers.keys())` — minor info disclosure. Either drop it or auth-guard `/readyz`.

---

## 9. Config Loading & Reload

### 9.1 Low: SIGHUP signal trust (`gateway/config.py:49-57`)

SIGHUP triggers a reload of whatever path is on disk. Anyone who can write to `config.yaml` and send SIGHUP can swap caller key hashes — same blast radius as filesystem write. Document the deployment expectation that the config file is read-only for the gateway user. Also note that even invalid configs are accepted as long as Pydantic validates them — there's no diff/confirmation step.

### 9.2 Info: `model_copy(update={"provider_mode": ...})` (`gateway/app.py:62-67`)

Pydantic `model_copy(update=...)` skips validators. Today `provider_mode` is `Literal["mock","real"]`, but with the new value coming from env there's no guarantee the env value is one of those — it'll fail later in `build_vendors` with a less clear message. Use `Config.model_validate({**cfg.model_dump(), **overrides})` instead.

---

## 10. Dependency Hygiene

- All dependencies are pinned (`pyproject.toml:7-20`) — good for reproducibility.
- `google-genai==0.3.0` is very early; SDK API surface is unstable; pinned is correct but expect churn.
- No `safety` / `pip-audit` step appears in CI.

**Recommendation:** add `pip-audit` to CI; renovate-bot for monthly upgrades.

---

## 11. Minor Findings / Nits

| Location | Note |
|---|---|
| `gateway/app.py:24` | `from gateway.auth import CallerResolver, hash_api_key` — `hash_api_key` is unused in `app.py`. |
| `gateway/app.py:28` | `from gateway.errors import ProviderError` — unused. |
| `gateway/app.py:196` | `eng` assigned but unused in `readyz`. |
| `gateway/router.py:65` | `tried: list[tuple[CandidateRef, str]]` is built but never serialized in the error body — debug aid only; consider including a sanitized version in logs. |
| `gateway/router.py:236` | `id=request_id or rec.vendor_req_id or "req"` — the literal `"req"` is a poor default; use `uuid4().hex`. |
| `gateway/ratelimit.py:79-100` | `remaining()` consumes 1 RPM as a side-effect; comment acknowledges this. Refactor to a dedicated read-only Lua script. |
| `gateway/breaker.py:171-191` | `scan_iter` over `gw:brk:*:samples:*` on every refresh — O(keys); fine at this scale, documented. |
| `gateway/providers/openai.py:62-64` | Double timeout (SDK `timeout=` plus `asyncio.wait_for`) — intentional belt-and-braces; document. |
| `gateway/db.py:153` | f-string interpolates `len(args)+1` — safe (integer), but easier to read if you `enumerate`. |
| `migrations/0001_init.sql` | No `enabled` column index on `callers` — fine for the current cardinality. |
| `tests/conftest.py` | Uses `fakeredis[lua]` — good. |

---

## 12. What's Already Done Well

- **Single boundary for outbound secrets** (`gateway/secrets.py`) — vendor adapters never read `os.environ` directly.
- **Vendor error taxonomy** (`gateway/errors.py`) cleanly bounds what the router has to know.
- **All Pydantic models in one place** with strict cross-validation (`Config._cross_validate_candidates_have_pricing_and_limits`).
- **Lua scripts for atomic two-dim bucket** with `EVALSHA`→`EVAL` fallback (`gateway/redis_state.py:115-126`).
- **Bounded async accounting queue** with explicit drop counter — backpressure is observable.
- **Bearer comparison via DB lookup on hash** — token never leaves memory.
- **Mock vendor scriptability** keeps the e2e tests fast and deterministic.
- **`provider_mode=real` doesn't crash on missing vendor keys** — partial deployment supported (`gateway/providers/__init__.py:56-72`).

---

## 13. Suggested Remediation Order

| Order | Finding | Effort | Risk if unaddressed |
|---|---|---|---|
| 1 | 2.2 Disable boot-time caller upsert in real mode | XS | Critical (dev key in prod) |
| 2 | 2.1 Rotate dev key + remove from repo history | S | Critical |
| 3 | 7.4 Copy `migrations/` into Docker image | XS | High (prod boot crash) |
| 4 | 3.3 Auth-guard `/metrics` or drop caller-labeled cost counters | S | High |
| 5 | 3.2 Restrict `/v1/usage` to the authenticated caller | XS | High |
| 6 | 2.3 Require `GATEWAY_DB_DSN` in real mode | XS | Medium |
| 7 | 4.1 Add `max_tokens` / message-size bounds | S | Medium |
| 8 | 3.4 Bound auth negative cache | S | Medium |
| 9 | 3.1 Move to HMAC-pepper or argon2id for API keys | M | Medium (long-term) |
| 10 | 4.2 Stop forwarding raw vendor error strings | S | Medium |
| 11 | 5.2 pg_advisory_xact_lock around migrations | XS | Medium |
| 12 | 5.4 / 7.1 Require TLS for Postgres, Redis, and inbound | M | Medium |
| 13 | Remaining low/info items | — | Low |

---

*End of report.*
