# `scripts/` — Operator-side tooling

## Purpose

This directory holds the **pre-deploy and smoke-test scripts** that the gateway no longer runs in its own boot path. Two responsibilities used to live inside `app.lifespan`:

1. Applying pending SQL migrations.
2. Seeding the `callers` table from a configuration source.

Both have been moved out of the request-serving process (commit `40eb4f6`) so that boot is a pure read-and-bind step and so that schema changes are gated behind an explicit operator action. This resolves cr-1 §5.2 (boot-time DDL) and cr-1 §7.4 (boot-time seed mutations). The gateway, today, refuses to mutate the database; it only reads it.

Everything under `scripts/` is therefore part of the **operator-facing surface**, not the runtime surface. CI runs the same scripts before bringing the gateway up; `docker-compose` does **not** invoke them automatically — see "Orchestration" below.

---

## Public surface

| Script | Purpose | Run by |
|---|---|---|
| [`scripts/setup.sh`](#setupsh--orchestrator) | One-shot orchestrator: migrations + seed. | Operator (manual), CI before integration tests. |
| [`scripts/apply_migrations.py`](#apply_migrationspy--migration-runner) | Apply pending SQL migrations under an advisory lock. | `setup.sh`, CI, or directly via `python -m scripts.apply_migrations`. |
| [`scripts/seed_callers.py`](#seed_callerspy--caller-seeder) | Upsert callers from JSON with HMAC-hashed keys. | `setup.sh`, CI. |
| [`scripts/real_provider_smoke.sh`](#real_provider_smokesh--live-smoke) | Curl the running gateway against real vendor keys. | Operator, post-deploy. |
| [`scripts/data/caller-seeding.json`](#callerseedingjson--seed-data) | Declarative caller list consumed by `seed_callers.py`. | Read-only; checked into the repo. |

---

## `setup.sh` — orchestrator

`scripts/setup.sh`. POSIX shell, `set -euo pipefail`. The whole script:

```sh
echo "applying migrations..."
python -m scripts.apply_migrations

echo "seeding callers..."
python -m scripts.seed_callers

echo "setup complete"
```

Both child scripts inherit the parent's environment. Required env vars (documented in the script header):

| Env var | Required by | Default |
|---|---|---|
| `GATEWAY_DB_DSN` | both children | `postgres://gateway:gateway@localhost:5432/gateway` (dev only — logs a warning) |
| `GATEWAY_KEY_HASH_PEPPER` | `seed_callers.py` | none — script aborts if unset/empty |
| `GATEWAY_SEED_KEY_<NAME>` | `seed_callers.py`, one per `name` in the JSON | none — script warns and skips that caller |

`pipefail` means a non-zero exit from either child aborts the whole script; `setup.sh`'s own exit status mirrors the first failing step.

This script is the canonical way to bring a fresh environment to a runnable state. It is **not** run automatically by `docker-compose up`; the gateway service has no `command:` override and starts in its `CMD ["uvicorn", ...]` immediately. The operator must run `./scripts/setup.sh` against the Postgres container before, or in parallel with, the gateway service coming up — see `scripts/real_provider_smoke.sh` header for the documented sequence.

---

## `apply_migrations.py` — migration runner

`scripts/apply_migrations.py`. Async entrypoint, env-driven (no `argparse`).

### Behavior

`main()` (`apply_migrations.py:23-46`):

1. Read `GATEWAY_DB_DSN` from the environment; default to the local-dev DSN and log a warning.
2. Import `gateway.db.Database` lazily so the script remains importable without `gateway` on `sys.path`.
3. `await db.connect()` — fail-fast with exit code 1 on connection error.
4. `await db.run_migrations()` — apply pending migrations.
5. `await db.close()` in a `finally`.

`Database.run_migrations()` (`gateway/db.py:52-`) opens a transaction, takes `pg_advisory_xact_lock(7331101)` so only one runner mutates schema at a time, then iterates the `.sql` files in `migrations/` in lexical order. The advisory lock is held for the duration of the transaction and released automatically on commit/rollback — see [`db.md`](db.md) for the full procedure.

Current migrations (`migrations/`):

| File | Purpose |
|---|---|
| `0001_init.sql` | Initial schema: `callers`, `requests`, indexes. |
| `0002_client_trace_id.sql` | Adds `requests.client_trace_id` column. |

### Invocation

Either form works; both go through the same `asyncio.run(main())` entrypoint at the bottom of the file (`apply_migrations.py:49-50`):

```sh
GATEWAY_DB_DSN=postgres://... python scripts/apply_migrations.py
python -m scripts.apply_migrations
```

### Exit codes

- `0` on success.
- `1` on DB connection failure or on any migration error (raised exception is logged via `log.error`, no traceback unless `GATEWAY_LOG_LEVEL=DEBUG`).

---

## `seed_callers.py` — caller seeder

`scripts/seed_callers.py`. Async, env-driven, idempotent (uses `Database.upsert_caller`).

### Behavior

`main()` (`seed_callers.py:69-95`):

1. Read `GATEWAY_KEY_HASH_PEPPER`; **abort with exit 1 if empty** — fail-loud, consistent with `gateway/auth.py` which also refuses empty peppers.
2. Read `GATEWAY_DB_DSN`; warn if defaulting.
3. `await db.connect()` — exit 1 on failure.
4. Call `seed_from_json(db, _SEEDING_JSON, dict(os.environ), pepper=pepper)`.
5. `await db.close()` in a `finally`.

`seed_from_json()` (`seed_callers.py:44-66`) is factored out so unit tests can substitute a stub `db`. Per JSON entry:

1. Compute `env_var = f"GATEWAY_SEED_KEY_{name.replace('-', '_').upper()}"`.
2. If that env var is unset, **print a `WARN` and skip** (this is normal in CI — only the callers the build needs get seeded).
3. Hash via `gateway.auth.hash_api_key(plaintext, pepper=pepper)`. The hash format is `v2:hmac-sha256:<hex>` (see `gateway/auth.py:22-26`). The function rejects an empty pepper at call time, providing a second safety net.
4. `await db.upsert_caller(name=..., key_hash=..., daily_token_cap=..., enabled=...)`.
5. Print `seeded caller=<name>`.

The script exits 0 even if every caller is skipped — that is a deliberate CI affordance, not a bug. The two hard failures (empty pepper, DB connection) exit 1.

### Caller name → env var mapping

```python
def _env_key_name(caller_name: str) -> str:
    normalized = caller_name.replace("-", "_").upper()
    return f"GATEWAY_SEED_KEY_{normalized}"
```

| Caller name in JSON | Looked-up env var |
|---|---|
| `dev` | `GATEWAY_SEED_KEY_DEV` |
| `search-svc` | `GATEWAY_SEED_KEY_SEARCH_SVC` |
| `analytics_pipeline` | `GATEWAY_SEED_KEY_ANALYTICS_PIPELINE` |

### Invocation

```sh
GATEWAY_KEY_HASH_PEPPER=<pepper> \
GATEWAY_SEED_KEY_DEV=<plaintext> \
python scripts/seed_callers.py
```

---

## `real_provider_smoke.sh` — live smoke

`scripts/real_provider_smoke.sh`. Bash, `set -euo pipefail`. Hits the running gateway with two `POST /v1/chat/completions` calls (one per logical tier, `fast` and `smart`) and then scrapes `/metrics` for evidence that an upstream actually served.

### Prerequisites (documented in the script header)

1. `cp .env.example .env` and fill in at least one real vendor key (`OPENAI_API_KEY` and/or `GOOGLE_API_KEY`).
2. `./scripts/setup.sh` must have already been run (migrations applied, dev caller seeded).
3. `docker compose up -d --build`.
4. Wait ~3 s for the gateway to come up.

### What it does

| Step | Endpoint | Assertion |
|---|---|---|
| 1 | `GET /healthz` | HTTP 200 (curl `-f`). |
| 2 | `GET /readyz` | HTTP 200. |
| 3 | `POST /v1/chat/completions` with `model: "fast"` | Body contains `"role":"assistant"`. |
| 4 | `POST /v1/chat/completions` with `model: "smart"` | Same assertion. |
| 5 | `GET /metrics` | Print every `gateway_attempts_total{...status="ok"...}` line (informational — no assertion beyond the grep). |
| 6 | `GET /metrics` | Print every `gateway_routing_weight` line so the operator sees per-candidate effective weights. |

### Configurable env vars

| Var | Default | Purpose |
|---|---|---|
| `GATEWAY_URL` | `http://localhost:8000` | Base URL the script hits. |
| `CALLER_KEY` | `dev-key-do-not-use-in-prod` | `Authorization: Bearer ...` value for the requests. Must match a seeded caller's plaintext key. |

### When it's run

After every deploy that touches a real vendor adapter or routing change. Not part of CI proper because it requires live vendor credentials.

---

## `caller-seeding.json` — seed data

`scripts/data/caller-seeding.json`. The file ships as:

```json
[
  {"name": "dev", "daily_token_cap": 1000000, "enabled": true}
]
```

### Schema (one entry per caller)

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | string | yes | — | Must match `^[a-z0-9_-]{1,64}$` (the `CallerName` regex from `gateway/models.py:31`). `seed_callers.py` does not re-validate, but `Database.upsert_caller` and the auth path do. |
| `daily_token_cap` | integer \| null | no | `null` | Forwarded to `upsert_caller(daily_token_cap=...)`. `null` = no cap. |
| `enabled` | bool | no | `true` | Forwarded; soft-disable flag. |

The JSON has no key material — plaintext keys live in environment variables only. To add a new seeded caller in CI: (1) append an entry to this JSON; (2) supply `GATEWAY_SEED_KEY_<NAME>` in the CI secrets store.

---

## Concurrency

- All three Python entrypoints are `asyncio.run(main())` — single event loop, single connection, no fan-out.
- The advisory lock in `apply_migrations` (`pg_advisory_xact_lock(7331101)`) serializes concurrent runners against the same Postgres database. The second runner blocks at `SELECT pg_advisory_xact_lock(...)` until the first commits or rolls back, then re-evaluates the migration table and applies nothing new.
- `seed_callers.py` has no cross-process serialization. Two concurrent runners against the same DB will race on `upsert_caller`, but since the operation is idempotent and the inputs come from the same JSON + env, the resulting row is the same. There is no harm beyond a possible duplicate log line.
- `setup.sh` is sequential by construction (`migrations; then seed`).

---

## Failure modes

| Failure | Where | Effect |
|---|---|---|
| `GATEWAY_DB_DSN` unset | `apply_migrations.py`, `seed_callers.py` | Default DSN used; warning logged. No abort. |
| Postgres unreachable | `db.connect()` | `log.error`, exit 1. |
| Migration SQL invalid | `db.run_migrations()` | Exception logged via `log.error`, exit 1. Advisory lock released on transaction rollback. |
| Two `apply_migrations` runners race | `pg_advisory_xact_lock` | Second runner blocks on the lock, then sees the migration is already applied and does nothing. |
| `GATEWAY_KEY_HASH_PEPPER` unset / empty | `seed_callers.py:70-77` | Hard fail with a one-line stderr message, exit 1. |
| `GATEWAY_SEED_KEY_<NAME>` missing for a caller | `seed_from_json` | `WARN: skipping <name>: ...` printed; loop continues. Exit 0. |
| `caller-seeding.json` malformed | `json.loads` in `seed_from_json` | Uncaught `json.JSONDecodeError` → exit 1. |
| `real_provider_smoke.sh` step fails | `curl -f` or grep | `set -e` aborts immediately with the failing step's exit code. |

---

## Configuration knobs

| Knob | Location | Effect |
|---|---|---|
| `GATEWAY_DB_DSN` | env | Postgres connection string for both Python scripts. |
| `GATEWAY_KEY_HASH_PEPPER` | env | HMAC pepper used by `gateway/auth.hash_api_key`. Must match the gateway's own pepper at runtime. |
| `GATEWAY_SEED_KEY_<NAME>` | env, one per caller | Plaintext API key handed to `hash_api_key`. |
| `GATEWAY_URL` | env (smoke) | Base URL for the live smoke test. |
| `CALLER_KEY` | env (smoke) | Bearer token for the live smoke test. |
| `migrations/*.sql` | repo | Ordered list of migrations to apply. Add new files with the next `NNNN_` prefix. |
| `scripts/data/caller-seeding.json` | repo | Declarative caller registry consumed by the seeder. |

---

## Orchestration

The relationship to runtime container plumbing:

| Surface | Runs `setup.sh`? | Notes |
|---|---|---|
| `Dockerfile` | **No.** `CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]` is the only entrypoint. The image does not bundle `scripts/` into its `COPY` lines (`Dockerfile:18-21`), so the gateway container cannot run them. |
| `docker-compose.yml` | **No.** No `command:` override on the `gateway` service. `depends_on` waits for Postgres + Redis health but does not run migrations. |
| Operator | **Yes.** Documented in `scripts/real_provider_smoke.sh` header: bring Postgres up via compose, run `./scripts/setup.sh` against it, then bring the gateway up. |
| CI | **Yes.** The integration-test job runs `./scripts/setup.sh` after the Postgres service is up and before any test that needs a seeded caller. |

This split is intentional: container images stay deterministic and runtime-only; schema and data mutations are gated on an explicit operator command. Reverting to boot-time migrations would re-open cr-1 §5.2 / §7.4.

---

## Open questions / known gaps

- **No drift check between vendored config and DB.** `config.yaml.callers` and the DB `callers` table are two sources of truth (the docs in [`config.md`](config.md) call this out). `seed_callers.py` writes to the DB; nothing writes to the YAML. Operators must remember to keep them aligned. A `--check` mode that compares the JSON to the live DB and exits non-zero on drift would help.
- **No migration rollback.** `apply_migrations.py` is forward-only. `Database.run_migrations()` has no `down` path; recovering from a bad migration requires a manual SQL diff against a backup. Acceptable for the current schema cadence (two files) but will need a real migration tool (alembic, dbmate, sqitch) once the schema starts churning monthly.
- **Seed keys live in env vars.** `GATEWAY_SEED_KEY_<NAME>` is convenient for CI secrets stores but invites accidental capture in shell history. A future iteration could read from a file path or stdin to avoid env-var exposure.
- **`real_provider_smoke.sh` asserts content, not provider.** Step 5 prints `gateway_attempts_total{...status="ok"...}` lines but does not assert which provider served. A flaky single-provider deploy could pass the smoke while routing 100% to one vendor.
- **No pepper rotation procedure.** `GATEWAY_KEY_HASH_PEPPER` is baked into the hash format (`v2:hmac-sha256:<hex>`). Rotating it requires reseeding every caller, but `seed_callers.py` has no `--rotate` mode that batches that.

---

## Cross-references

- [`db.md`](db.md) — `Database.run_migrations`, `pg_advisory_xact_lock`, `upsert_caller` semantics.
- [`auth.md`](auth.md) — `hash_api_key` and the `v2:hmac-sha256:` format.
- [`config.md`](config.md) — `Config.callers` (the YAML view of the same registry; see drift note above).
- [`app.md`](app.md) — confirms `app.lifespan` no longer mutates the database; the boot path only reads.
