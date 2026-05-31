# `gateway/secrets.py` — outbound credential boundary

## Purpose

`SecretsManager` is the single abstraction through which the gateway reads
*outbound* and *server-side-only* credentials — vendor API keys
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`), the
`GATEWAY_METRICS_TOKEN` used to gate `/metrics`, and the
`GATEWAY_KEY_HASH_PEPPER` used by `gateway/auth.py` to HMAC inbound caller
bearer tokens. Two concrete implementations are provided: `EnvSecretsManager`
for production (reads `os.environ`) and `MockSecretsManager` for dev and tests
(in-memory dict).

This module does *not* handle the storage of inbound caller bearer keys —
those are HMAC-hashed (using the pepper from this module) and stored in
Postgres; see [`auth.md`](auth.md) and [`db.md`](db.md). The asymmetry is by
design: outbound credentials and the pepper are operator-owned and small in
number; inbound caller credentials are caller-owned and grow with the user
base.

The contract is also the *only* place vendor adapters look up keys. Vendor
adapters never touch `os.environ` directly. See [`modules/providers.md`](providers.md).

## Public surface

| Symbol | Signature | Notes |
|---|---|---|
| `SecretsManager` | `ABC` | Two abstract methods: `get`, `has`. |
| `SecretsManager.get` | `(key: str) -> str` | Returns the value. Raises `KeyError` if absent. |
| `SecretsManager.has` | `(key: str) -> bool` | Returns presence. Must not raise. |
| `EnvSecretsManager` | `SecretsManager` subclass | Reads `os.environ` at call time. |
| `MockSecretsManager` | `SecretsManager` subclass | In-memory dict; adds a `set(key, value)` mutator. |
| `MockSecretsManager.__init__` | `(seed: dict[str, str] \| None = None)` | Optional starter dict. |
| `MockSecretsManager.set` | `(key: str, value: str) -> None` | Test/dev helper. |
| `build_secrets_manager` | `(mode: SecretsMode) -> SecretsManager` | Factory used at boot. |

`SecretsMode` is a `Literal["mock", "env"]` defined in `gateway/models.py`.
It is one of the env-var-overridable values on `Config` (see `app.py`).

## Internals

### The `get` / `has` contract

| Method | On present | On absent | On any other failure |
|---|---|---|---|
| `get(key)` | returns `str` | raises `KeyError(f"secret {key!r} not …")` | (no other failure mode in either impl) |
| `has(key)` | returns `True` | returns `False` | **must not raise** |

`has` is intentionally non-raising because callers use it to *decide* whether to
attempt `get`. The pattern in vendor adapters is `if secrets.has(KEY):
adapter_can_be_built = True` — see `gateway/providers/__init__.py::build_vendors`.
A `has` that raised would force every caller to wrap it in a `try`/`except` and
the asymmetry between "secret missing" and "secret store broken" would be lost.

### `EnvSecretsManager`

`secrets.py:29-44`:

```python
def get(self, key: str) -> str:
    value = os.environ.get(key, "")
    if not value:
        raise KeyError(f"secret {key!r} not set in environment")
    return value

def has(self, key: str) -> bool:
    return bool(os.environ.get(key, ""))
```

Two non-obvious properties:

- **Reads `os.environ` at call time, not at construction time.** This matters
  for test harnesses that mutate the environment after the manager is built,
  and for processes that receive late-bound secrets (e.g. an init container
  writing to `/proc/<pid>/environ`-equivalents). It is not a performance hot
  path — vendor adapters cache their constructed clients.
- **Empty string is treated as absent.** This is the docker-compose
  `${FOO:-}` problem: an unset env var rendered through a `:-` substitution
  becomes the empty string, and an empty string is never a usable API key.
  Treating it as absent makes the "I forgot to set this" failure mode loud
  (`KeyError`) instead of silent (vendor adapter calls `Authorization: Bearer `).

### `MockSecretsManager`

In-memory dict. `get` raises `KeyError` on miss with a message distinguishing
"not present in MockSecretsManager" from the env-backed message — useful when a
test failure log surfaces a `KeyError`. `set` is a test/dev helper for seeding
secrets after construction.

The boot path in `app.py:156-169` seeds the mock manager from `os.environ` for
the five well-known keys (vendor keys + metrics token + auth pepper):

```python
if cfg.secrets_mode == "mock":
    assert isinstance(secrets, MockSecretsManager)
    for _key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
        _METRICS_TOKEN_KEY, "GATEWAY_KEY_HASH_PEPPER",
    ):
        _val = os.environ.get(_key, "")
        if _val:
            secrets.set(_key, _val)
```

This lets dev environments supply real keys via env without flipping
`secrets_mode` to `"env"` (which would also disable any other dev affordances).

### `build_secrets_manager`

Trivial factory:

```python
def build_secrets_manager(mode: SecretsMode) -> SecretsManager:
    if mode == "mock":
        return MockSecretsManager()
    if mode == "env":
        return EnvSecretsManager()
    raise ValueError(f"unknown secrets_mode: {mode!r}")
```

`mode` comes from the validated `Config.secrets_mode`. Validation happens at
`Config.model_validate` time (see [`config.md`](config.md)), so the `ValueError`
branch is only reached if someone bypasses Pydantic. It is kept as a
defense-in-depth guard.

### Boot wiring & shared handle

The constructed `SecretsManager` is held on `app.state.secrets`
(`app.py:219`). This is the canonical handle:

- The `/metrics` route reads `GATEWAY_METRICS_TOKEN` via
  `request.app.state.secrets.has(...)` / `.get(...)` to gate access
  (fail-closed if the token isn't configured).
- `gateway/auth.py` reads `GATEWAY_KEY_HASH_PEPPER` via this manager at
  boot (`pepper = secrets.get("GATEWAY_KEY_HASH_PEPPER")`, `app.py:171`)
  and passes it into `CallerResolver(db=db, pepper=pepper)`. A missing
  pepper raises `KeyError` from `get` and the lifespan fails loud.
- Vendor adapters consume the manager via `build_vendors(cfg, secrets)`
  at `app.py:173`.

### Well-known keys

| Key | Consumer | Behavior on miss |
|---|---|---|
| `OPENAI_API_KEY` | `build_vendors` → OpenAI adapter | Adapter skipped; tier candidate becomes unrouteable. |
| `ANTHROPIC_API_KEY` | `build_vendors` → Anthropic adapter | Same. |
| `GOOGLE_API_KEY` | `build_vendors` → Google adapter | Same. |
| `GATEWAY_METRICS_TOKEN` | `/metrics` route handler | `/metrics` returns 401 (fail-closed). |
| `GATEWAY_KEY_HASH_PEPPER` | `gateway/auth.py` (HMAC of inbound bearer tokens) and `scripts/seed_callers.py` (hashing caller plaintext keys before upsert) | Lifespan crashes at boot with `KeyError`. There is no graceful degradation — without a pepper, inbound auth cannot work. |

## Concurrency model

- **Stateless and thread-safe.** `EnvSecretsManager` keeps no state at all; each
  call reads `os.environ`. `MockSecretsManager` keeps a dict but is only mutated
  via `set()` during boot (`app.py:156-169`) and via test fixtures —
  not during request serving.
- **No locks.** Vendor adapters read the secret at construction time (during
  `build_vendors`); after boot, the in-flight read pattern is `has`/`get` on a
  steady map, which under CPython's GIL is safe.
- **No I/O.** Neither implementation touches a network or a file — they read
  process memory only. This is why the contract can be sync rather than async.

## Failure modes

| Scenario | Behavior |
|---|---|
| `get(key)` and the secret is absent (`EnvSecretsManager`, env unset) | `KeyError("secret {key!r} not set in environment")`. |
| `get(key)` and the env var is set to `""` | Same `KeyError`. Empty string is treated as absent. |
| `get(key)` and the secret is absent (`MockSecretsManager`) | `KeyError("secret {key!r} not present in MockSecretsManager")`. |
| `has(key)` and any of the above | Returns `False`. Does not raise. |
| `build_secrets_manager` with a non-Literal mode | `ValueError`. Only reachable if Pydantic is bypassed. |
| Vendor adapter starts without its required key | `build_vendors` skips the adapter; the candidate becomes effectively unrouteable and the Router will pick something else. See [`modules/providers.md`](providers.md). The gateway does *not* fail to boot — a partial fleet is supported. |
| `GATEWAY_KEY_HASH_PEPPER` not set | `secrets.get("GATEWAY_KEY_HASH_PEPPER")` raises `KeyError` at boot (`app.py:171`); the lifespan aborts. There is no degraded mode. |
| `/metrics` requested without `GATEWAY_METRICS_TOKEN` set | `request.app.state.secrets.has(_METRICS_TOKEN_KEY)` returns `False` → `/metrics` returns 401 (fail-closed). |

## Configuration knobs

| Knob | Source | Default | Effect |
|---|---|---|---|
| `secrets_mode` | `config.yaml` top-level, overridable by `GATEWAY_SECRETS_MODE` env | (no global default; must be set in config) | Selects implementation. `"mock"` → `MockSecretsManager`; `"env"` → `EnvSecretsManager`. |
| Seeded keys (mock mode only) | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `GATEWAY_METRICS_TOKEN`, `GATEWAY_KEY_HASH_PEPPER` env vars | unset | Copied into the `MockSecretsManager` at boot (`app.py:156-169`). |
| Keys (env mode) | Whatever env vars the vendor adapters, `/metrics` handler, and the auth pepper loader look up | unset | Read at call time. |

The set of keys is not enumerated by this module — each consumer asks for what
it needs. The well-known set at the time of writing is the five above.

## Open questions / known gaps

- **No external secret-manager integration.** AWS Secrets Manager, GCP Secret
  Manager, Vault, etc. are not supported as backends. The `SecretsManager` ABC
  is shaped to make adding one straightforward (`get`/`has` are the only
  surface), but no such adapter exists.
- **No rotation hook.** `EnvSecretsManager.get` reads `os.environ` per call, so
  in principle an external sidecar that mutates the process env can rotate
  keys. In practice, vendor adapter clients and the `CallerResolver`'s
  pepper are captured once at boot — rotation requires a process restart.
  Rotating `GATEWAY_KEY_HASH_PEPPER` additionally requires re-seeding the
  `callers` table (see [`auth.md`](auth.md) — Open questions).
- **`SecretsManager` is sync.** An async backend (HTTP-based secret fetch)
  would either need to block the event loop or refactor the ABC to async. The
  pragma today is "if you need async, cache it sync at boot."
- **No audit logging on `get`.** Successful reads are silent. A high-security
  environment might want a per-`get` log line for forensics.
- **Test coverage** ([`../code-review/t-1.md`](../../code-review/t-1.md)):
  `tests/test_secrets.py` covers the empty-string-as-absent contract, the
  factory's mode dispatch, and both `get`/`has` happy + miss paths.

## Cross-references

- Vendor adapter consumers (the primary callers): [`modules/providers.md`](providers.md)
  describes how `build_vendors` uses `SecretsManager.has` to skip unconfigured
  vendors and how each adapter does `get(KEY)` at construction.
- `/metrics` gate: [`modules/app.md`](app.md) — reads
  `request.app.state.secrets`.
- Auth pepper consumer: [`modules/auth.md`](auth.md) —
  `GATEWAY_KEY_HASH_PEPPER` is loaded from this manager at
  `app.py:171` and feeds `hash_api_key(...)` and the `CallerResolver`.
- Caller seeding: `scripts/seed_callers.py` requires the same
  `GATEWAY_KEY_HASH_PEPPER` (read directly from env, since the script
  runs out-of-process before the app lifespan); it hashes each
  plaintext from `GATEWAY_SEED_KEY_<NAME>` and upserts into `callers`.
- Config knob: [`modules/config.md`](config.md) — `SecretsMode` literal and the
  `GATEWAY_SECRETS_MODE` env override flow.
- Architecture context: [`architecture.md`](../architecture.md) §4 row
  "Outbound vendor API keys" — restates the single-boundary invariant.
- Caller-key (inbound) hash storage — different system, do not confuse:
  [`modules/auth.md`](auth.md), [`modules/db.md`](db.md).
