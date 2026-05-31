# `gateway/db.py` — Postgres pool, schema, and queries

## Purpose

Owns the gateway's only persistent datastore. `Database` wraps an `asyncpg.Pool`
and exposes the four shapes of access the rest of the service needs: schema
bring-up (`run_migrations`), batched audit-log writes (`write_batch`), the caller
registry (`upsert_caller`, `caller_by_key_hash`), and the read paths used by
`/v1/chat/completions` enforcement (`caller_tokens_used_today`) and `/v1/usage`
(`usage_summary`).

This module is the *only* place in `gateway/` that imports `asyncpg`. Every
other component that needs persistence talks to a `Database` instance or a
narrower Protocol (see `accounting.py` `BatchedWriter`, `auth.py` `_DBProtocol`).

See [`architecture.md`](../architecture.md) §4 for how Postgres fits into the
overall state partitioning, and [`modules/app.md`](app.md) for boot-order
wiring.

## Public surface

| Symbol | Signature | Notes |
|---|---|---|
| `Database` | `class` | The only public class. |
| `Database.__init__` | `(*, dsn: str, min_size: int = 1, max_size: int = 5)` | Pool sizing knobs; DSN passed straight to `asyncpg.create_pool`. |
| `Database.connect` | `async () -> None` | Idempotent: no-op if already connected. |
| `Database.close` | `async () -> None` | Idempotent: resets `_pool` to `None`. |
| `Database.pool` | `property -> asyncpg.Pool` | Raises `RuntimeError` if `connect()` not called. |
| `Database.run_migrations` | `async () -> None` | Applies every `migrations/*.sql` inside one transaction, gated by `pg_advisory_xact_lock`. Not invoked from `gateway/app.py` lifespan; ops runs it via `scripts/apply_migrations.py`. |
| `Database.write_batch` | `async (records: list[AttemptRecord]) -> None` | One `executemany` per call. No-op on empty list. |
| `Database.upsert_caller` | `async (*, name, key_hash, daily_token_cap, enabled=True) -> None` | `INSERT … ON CONFLICT (name) DO UPDATE`. |
| `Database.caller_by_key_hash` | `async (key_hash: str) -> Caller \| None` | Used by `CallerResolver`. |
| `Database.caller_tokens_used_today` | `async (caller: str) -> int` | Sums `input_tokens + output_tokens` since UTC midnight. |
| `Database.usage_summary` | `async (*, caller=None, since=None) -> list[dict]` | Aggregates by `(caller, provider, model)`. |

`Database` satisfies the `BatchedWriter` Protocol from `accounting.py` by virtue
of `write_batch`. It also satisfies the `_DBProtocol` from `auth.py` by virtue
of `caller_by_key_hash`. No explicit base class is involved — duck typing is the
only contract.

## Internals

### Schema

The current head schema, materialized by `migrations/0001_init.sql` and
`migrations/0002_client_trace_id.sql`:

```sql
CREATE TABLE IF NOT EXISTS requests (
  id              BIGSERIAL PRIMARY KEY,
  request_id      TEXT NOT NULL,
  caller          TEXT NOT NULL,
  tier            TEXT NOT NULL,
  provider        TEXT NOT NULL,
  model           TEXT NOT NULL,
  attempt_idx     SMALLINT NOT NULL,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  cost_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
  latency_ms      INTEGER NOT NULL,
  status          TEXT NOT NULL,
  vendor_req_id   TEXT,
  ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  client_trace_id TEXT                              -- added in 0002
);

CREATE INDEX IF NOT EXISTS requests_caller_ts_idx ON requests (caller, ts DESC);
CREATE INDEX IF NOT EXISTS requests_ts_idx        ON requests (ts DESC);

CREATE TABLE IF NOT EXISTS callers (
  name             TEXT PRIMARY KEY,
  key_hash         TEXT NOT NULL,
  daily_token_cap  BIGINT,
  enabled          BOOLEAN NOT NULL DEFAULT TRUE
);
```

#### `requests` columns

| Column | Type | Filled by | Notes |
|---|---|---|---|
| `id` | `BIGSERIAL` | DB default | Surrogate PK. No business meaning. |
| `request_id` | `TEXT` | `Router` (per inbound request) | Server-generated; same across all attempts of one request; correlates with logs. |
| `caller` | `TEXT` | `CallerResolver` → `Caller.name` | Matches `callers.name` but no FK (audit log must survive caller removal). |
| `tier` | `TEXT` | `ChatCompletionRequest.model` | The *logical* tier name (`fast`, `smart`), not a vendor model. |
| `provider` | `TEXT` | Router after candidate pick | Vendor key (`openai`, `anthropic`, `google`). |
| `model` | `TEXT` | Router after candidate pick | Vendor's model id. |
| `attempt_idx` | `SMALLINT` | Router loop counter | 0-indexed; non-zero values indicate failover attempts. |
| `input_tokens` | `INTEGER` | Vendor response (`ChatResult.input_tokens`) | 0 for failed attempts. |
| `output_tokens` | `INTEGER` | Vendor response | 0 for failed attempts. |
| `cost_usd` | `NUMERIC(12,6)` | Router (price-table lookup × tokens) | Pre-aggregated per attempt. |
| `latency_ms` | `INTEGER` | Router (wallclock around vendor call) | Includes per-attempt timeout. |
| `status` | `TEXT` | Router | `"ok"` or a `ProviderErrorKind` value (`rate_limited`, `transient_5xx`, …). |
| `vendor_req_id` | `TEXT` | Vendor (`ChatResult.vendor_request_id`) | Nullable; some adapters don't expose one. |
| `ts` | `TIMESTAMPTZ` | DB default `NOW()` | Used by `caller_tokens_used_today` and `usage_summary.since`. |
| `client_trace_id` | `TEXT` NULL | `AttemptRecord.client_trace_id` | Caller-supplied trace id (distinct from the server-generated `request_id`). Pydantic enforces `max_length=128` on the wire; column itself is unconstrained `TEXT`. Added by `migrations/0002_client_trace_id.sql`. |

#### `callers` columns

| Column | Type | Filled by | Notes |
|---|---|---|---|
| `name` | `TEXT PRIMARY KEY` | `scripts/seed_callers.py` or admin tooling | Matches `^[a-z0-9_-]{1,64}$`. |
| `key_hash` | `TEXT NOT NULL` | `hash_api_key(raw_key, pepper=...)` (see [`auth.md`](auth.md)) | `v2:hmac-sha256:<hex>` format. |
| `daily_token_cap` | `BIGINT` (nullable) | Operator | `NULL` = unlimited. |
| `enabled` | `BOOLEAN NOT NULL DEFAULT TRUE` | Operator | `CallerResolver` treats disabled as "not found". |

Note there is no UNIQUE constraint on `callers.key_hash`. Two callers can in
principle share a hash; `caller_by_key_hash` does a single-row `fetchrow` and
returns whichever Postgres yields first. Operationally this is enforced
out-of-band by the seeding script.

### Migrations (`run_migrations`)

`db.py:52-79`:

```python
if not _MIGRATIONS_DIR.exists():
    raise RuntimeError(f"migrations directory not found: {_MIGRATIONS_DIR}. ...")
files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
async with self.pool.acquire() as conn:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", 7331101)
        for f in files:
            await conn.execute(f.read_text())
```

Properties:

- **Ordering** — `sorted()` over the glob gives a stable, name-based order
  (`0001_…`, `0002_…`, `…`). Names must be chosen accordingly.
- **Atomicity** — single transaction. If any file fails to apply, *none* of the
  migrations in this run are committed.
- **Idempotency** — written into each migration: every `CREATE` is
  `IF NOT EXISTS`, every `ALTER TABLE … ADD COLUMN` is `IF NOT EXISTS`. Re-runs
  are safe and tests call this freely (`tests/test_db.py::test_migrations_idempotent`).
- **Concurrency across replicas** — guarded by a Postgres transactional advisory
  lock (`pg_advisory_xact_lock(7331101)`, `db.py:75`). The lock id is an
  arbitrary project-specific constant. The lock is released automatically when
  the transaction commits or rolls back. Two replicas/scripts starting up in
  parallel will serialize at this point; the second one re-applies the same
  idempotent SQL, which is a no-op.
- **Fails loud on missing/empty `migrations/` directory** — `run_migrations`
  raises a descriptive `RuntimeError` if the directory does not exist
  (`db.py:55-59`) or contains no `*.sql` files (`db.py:61-65`). This catches a
  Dockerfile gap where the image was built without copying `migrations/`.

**`gateway/app.py` lifespan does NOT call `run_migrations`** in normal
startup. Ops applies migrations as a separate pre-deploy step via
`scripts/apply_migrations.py` (which is orchestrated by `scripts/setup.sh`).
`Database.run_migrations` remains a public method so test fixtures and
scripts can call it directly; the app boot only verifies the `callers`
table is non-empty and logs a warning if not (`app.py:142-149`).

### `write_batch`

The accounting hot-path. Receives a `list[AttemptRecord]`, materializes each
as a 13-tuple (now including `client_trace_id`), and issues one
`conn.executemany(INSERT …, rows)`. There is no `RETURNING` and no per-row
error handling — if any row fails (e.g. constraint violation) the entire
batch raises and the `AccountingQueue` catches it (see
[`accounting.md`](accounting.md) "Failure modes").

`db.py:104-115`:

```sql
INSERT INTO requests
    (request_id, caller, tier, provider, model,
     attempt_idx, input_tokens, output_tokens, cost_usd,
     latency_ms, status, vendor_req_id, client_trace_id)
VALUES
    ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
```

Empty-list calls short-circuit without acquiring a connection (`db.py:84-85`).

### `caller_tokens_used_today`

```python
today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
SELECT COALESCE(SUM(input_tokens + output_tokens), 0)::BIGINT
FROM requests
WHERE caller = $1 AND ts >= $2
```

Called on every inbound `/v1/chat/completions` for callers whose
`daily_token_cap` is set. The `(caller, ts DESC)` index covers it.

"Today" is UTC; a caller exhausted at 23:59 UTC is unblocked at 00:00 UTC. There
is no per-caller timezone preference.

### `usage_summary` query builder

`db.py:164-190` constructs a parameterized SQL string with three possible
branches:

| `caller` | `since` | `WHERE` clause |
|---|---|---|
| `None` | `None` | (no `WHERE`) — full table scan, aggregated. |
| set | `None` | `WHERE caller = $1` |
| `None` | set | `WHERE ts >= $1` |
| set | set | `WHERE caller = $1 AND ts >= $2` |

Placeholder numbering is computed from `len(args)` *before* the arg is appended,
so the indices stay consistent regardless of which optional filters are
provided. The `GROUP BY` is `(caller, provider, model)` and `ORDER BY` the same.

The endpoint that calls this — `GET /v1/usage` in `app.py` — always passes
`caller=resolved.name` to enforce that callers only see their own usage,
per [`../code-review/cr-1.md`](../../code-review/cr-1.md) §3.2.

## Concurrency model

- **Pool sizing**: `min_size=1`, `max_size=5` by default. These are passed
  straight to `asyncpg.create_pool`. Each request handler that needs Postgres
  briefly acquires a connection via `async with self.pool.acquire()` and
  releases it on exit. There is no long-held connection.
- **Single in-process instance**: `Database` is constructed once in
  `app.lifespan` and stored on `app.state.db` (`app.py:139-140`, exposed via
  `app.state.db` at `app.py:213`). Every consumer (`CallerResolver`, the
  `/v1/chat/completions` daily-cap check, the `AccountingQueue` writer slot,
  `/v1/usage`) shares this instance.
- **`asyncpg` thread-safety**: not used. Everything runs on the asyncio event
  loop. The pool serializes connection handoff between awaiting tasks.
- **Concurrent batch writes are safe**: each `write_batch` call acquires its own
  connection. The `requests` table has no uniqueness constraint that could
  cause an `executemany` to conflict with another in-flight batch.
- **`run_migrations` under concurrency**: see "Migrations" above — the advisory
  lock serializes concurrent runs (across `apply_migrations.py` invocations
  or test fixtures).

## Failure modes

| Scenario | Behavior |
|---|---|
| Postgres unreachable at `connect()` | `asyncpg.create_pool` raises → uvicorn fails to start lifespan → process exits. |
| Postgres unreachable mid-flight from `write_batch` | Exception propagates to `AccountingQueue._flush`, which catches it, increments `_dropped_total`, and bumps `ACCOUNTING_DROPPED` live. The user request itself already succeeded. See [`accounting.md`](accounting.md). |
| `caller_by_key_hash` raises | Propagates through `CallerResolver.resolve_bearer`; the FastAPI handler converts to 500. The negative-cache entry is *not* set, so retries re-hit the DB. |
| `caller_tokens_used_today` raises | Propagates as a 500 from `/v1/chat/completions`; the user's request is rejected before any vendor call. |
| `pool` accessed before `connect()` | `db.py:46-47`: `if self._pool is None: raise RuntimeError("Database.connect() not called")`. Plain `raise`, not an `assert`, so it survives under `python -O`. |
| Migration file fails to apply | Single-transaction abort: no schema changes are committed. The exception propagates and `apply_migrations.py` exits non-zero. |
| Migrations directory missing or empty | `RuntimeError` from `run_migrations` (`db.py:55-65`) with an explicit pointer to the Docker image gap. |

### Known sharp edges

- **§5.2 — concurrent-migration race.** Resolved in commit `cc67f12`:
  `run_migrations` now takes `pg_advisory_xact_lock(7331101)` inside the
  transaction so parallel applications serialize. Same commit makes the
  function fail loud if the `migrations/` directory is missing.
- **§5.3 — `assert self._pool is not None` strippable under `python -O`.**
  Resolved: the `pool` property uses an explicit
  `raise RuntimeError("Database.connect() not called")` (`db.py:46-47`).
- **§5.4 — no TLS configured on the DSN.** Open. `asyncpg.create_pool(dsn=...)`
  honours `sslmode` only if the DSN includes it. A boot-time warning is
  emitted from `gateway/app.py` if `provider_mode=real` and the DSN lacks
  `sslmode=require` / `sslmode=verify`; see [`app.md`](app.md) for the
  warning site. The warning does not block startup.
- **No `COPY` for batched inserts** ([`cr-1.md`](../../code-review/cr-1.md) §5.5).
  `executemany` is used for clarity; at expected throughput (≤ batch of 200
  every 250 ms) it is comfortably within budget.
- **`requests` grows unbounded** ([`cr-1.md`](../../code-review/cr-1.md) §5.6).
  No retention job. Operators are expected to attach a partitioning / TTL
  policy out-of-band.

## Configuration knobs

| Knob | Source | Default | Effect |
|---|---|---|---|
| DSN | `GATEWAY_DB_DSN` env var (read in `app.py:109-116`) | none in real mode; `postgres://gateway:gateway@localhost:5432/gateway` in mock mode | `Database(dsn=...)`. In `provider_mode=real`, missing this is a startup error. |
| Pool min size | `Database(min_size=...)` constructor | `1` | `asyncpg.create_pool(min_size=...)`. Not externally configurable today. |
| Pool max size | `Database(max_size=...)` constructor | `5` | Same. Limits concurrent DB ops per replica. |
| Migrations directory | `_MIGRATIONS_DIR` (module constant) | `<repo>/migrations` | Resolved as `Path(__file__).resolve().parent.parent / "migrations"`. |

There are no env-var overrides for pool sizing today. Tuning requires a code
change.

## Open questions / known gaps

- **Pool sizing is hardcoded.** A `max_size=5` per replica may be tight at
  higher concurrency; raising it requires either exposing it via env or
  changing the call site in `app.py:139`.
- **`requests` retention is a manual operator concern.**
  [`cr-1.md`](../../code-review/cr-1.md) §5.6 suggests time-based partitioning;
  this is not implemented.
- **No `client_trace_id` index.** The two existing indexes are
  `(caller, ts DESC)` and `(ts DESC)`. Querying by trace id is a sequential
  scan today.
- **No FK from `requests.caller` to `callers.name`.** Intentional: the audit
  log must survive caller deletion. There is no scheduled "reconcile" job.
- **No TLS enforcement on the DSN** ([`cr-1.md`](../../code-review/cr-1.md) §5.4).
  The boot-time warning in `gateway/app.py` flags missing
  `sslmode=require`/`verify-full` in real mode, but startup still proceeds.
- **Test coverage gaps** ([`../code-review/t-1.md`](../../code-review/t-1.md) §5):
  `test_db.py` is skipped without a live Postgres; the advisory-lock contention
  and concurrent-`write_batch` paths are not exercised in CI.

## Cross-references

- Boot wiring: [`modules/app.md`](app.md) (notably the TLS boot-warning at
  `app.py:118-127` and the empty-callers warning at `app.py:142-149`).
- Pre-deploy migration & seeding: `scripts/apply_migrations.py`,
  `scripts/seed_callers.py`, orchestrated by `scripts/setup.sh`.
- Architecture context: [`architecture.md`](../architecture.md) §4 (state partitioning), §7 (resilience)
- Consumers of `write_batch`: [`modules/accounting.md`](accounting.md)
- Consumers of `caller_by_key_hash`: [`modules/auth.md`](auth.md)
- Code-review references: [`../code-review/cr-1.md`](../../code-review/cr-1.md) §5.4 (open), §5.5 (open), §5.6 (open), §7.4 (resolved cc67f12)
- Test review: [`../code-review/t-1.md`](../../code-review/t-1.md) §5
