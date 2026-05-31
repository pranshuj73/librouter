# Config + Models

`gateway/config/__init__.py` + `gateway/models.py`

## Purpose

This module owns the **single source of typed configuration** for the gateway and the **single home for every Pydantic `BaseModel`** in the codebase (`gateway/models.py:1-7`). It does three jobs:

1. Parse `config.yaml` into a validated `Config` object at boot (`load_config`, `gateway/config/__init__.py:23-26`).
2. Hold that object behind a `ConfigHolder` so consumers can read the latest value after a SIGHUP atomic swap (`gateway/config/__init__.py:29-46`).
3. Define every wire-level (OpenAI-compatible) and internal DTO type the rest of the gateway exchanges (`gateway/models.py:123-267`).

Convention enforced by this file: **Pydantic models live nowhere else.** ABCs, Protocols, dataclasses, and pure-enum types live in their own modules.

### Package layout note

`gateway/config` is a **Python package**, not a single module (refactored in commit `0323080`). The import path is unchanged — `from gateway.config import load_config` still works — but the file on disk is now `gateway/config/__init__.py`. The package directory also contains `logger.json`, a redaction-pattern table consumed by `gateway/logging.py`; see [`observability.md`](observability.md) for its schema and reload behavior.

Consumer: [`modules/app.md`](app.md) (loads + holds + applies env overrides), [`modules/providers.md`](providers.md) (reads `provider_mode`), [`modules/secrets.md`](secrets.md) (reads `secrets_mode`), [`modules/routing.md`](routing.md) (reads `RoutingConfig`).

## Public surface

| Symbol | Kind | Defined at | Notes |
|---|---|---|---|
| `load_config(path)` | function | `config/__init__.py:23` | Read YAML, parse via `Config.model_validate`. Raises `pydantic.ValidationError` on schema or cross-validation failure. |
| `ConfigHolder` | class | `config/__init__.py:29` | Mutable holder. Consumers store a reference to the holder, not the `Config`; they always read `holder.value`. |
| `ConfigHolder.reload()` | method | `config/__init__.py:37` | Re-read `source_path` and atomically replace `.value`. On failure logs and keeps old value. |
| `install_sighup_reload(holder)` | function | `config/__init__.py:49` | Registers SIGHUP handler. No-op on platforms without SIGHUP (Windows) or off-main-thread. |
| `Config` | Pydantic model | `models.py:94` | Top-level YAML schema. `extra="forbid"`. |
| `TierEntry`, `PriceEntry`, `RateLimitEntry`, `CallerEntry`, `RoutingConfig` | Pydantic models | `models.py:55-91` | Nested config types. |
| `ChatCompletionRequest`, `Message`, `ChatCompletionResponse`, `Choice`, `Usage`, `ErrorBody` | Pydantic models | `models.py:126-207` | OpenAI-shaped wire contract. |
| `ChatParams`, `ChatResult`, `CandidateRef`, `AttemptRecord`, `Caller` | Pydantic models | `models.py:213-266` | Internal DTOs. |
| `ProviderErrorKind` | `str` Enum | `models.py:37-45` | Normalized error taxonomy emitted by Vendor adapters. |
| `CallerName` | Annotated type | `models.py:31` | `^[a-z0-9_-]{1,64}$`; reused by `CallerEntry`, `Caller`, `AttemptRecord.caller`. |
| `ProviderMode`, `SecretsMode` | Literal aliases | `models.py:48-49` | `mock | real`, `mock | env`. |

---

## Top-level schema

`Config` is defined at `gateway/models.py:94-120`. It has `model_config = ConfigDict(extra="forbid")` — any unknown top-level key fails validation.

| Field | Type | Default | Description | Validation | Source |
|---|---|---|---|---|---|
| `provider_mode` | `Literal["mock", "real"]` | (required) | Which `Vendor` family `build_vendors` instantiates. `mock` returns deterministic fakes; `real` uses vendor SDKs. | Literal | `models.py:99` |
| `secrets_mode` | `Literal["mock", "env"]` | (required) | Which `SecretsManager` `build_secrets_manager` returns. `mock` is in-memory; `env` reads `os.environ`. | Literal | `models.py:100` |
| `tiers` | `dict[str, list[TierEntry]]` | (required) | Logical tier → ordered list of candidates. Tier names are the values callers pass as the OpenAI `model` field. | Cross-validated against `prices` and `rate_limits` (see below). | `models.py:101` |
| `routing` | `RoutingConfig` | `RoutingConfig()` | Routing/refresh tuning. Optional in YAML — defaults applied if absent. | Nested model. | `models.py:102` |
| `prices` | `dict[str, PriceEntry]` | (required) | Key is `"<provider>/<model>"`. USD per 1M tokens. | Every candidate key in `tiers` must have an entry. | `models.py:103` |
| `rate_limits` | `dict[str, RateLimitEntry]` | (required) | Same key shape as `prices`. Per-minute fleet-wide caps. | Every candidate key in `tiers` must have an entry. | `models.py:104` |
| `callers` | `list[CallerEntry]` | (required, may be empty) | Static caller registry. Loaded once at boot; runtime auth still lives in Postgres `callers` table. In dev configs this may be `[]` (`config.dev.yaml:40`). | List of nested. | `models.py:105` |

### `TierEntry`

`gateway/models.py:55-60`. A single candidate inside a tier.

| Field | Type | Default | Description |
|---|---|---|---|
| `provider` | `str` | (required) | Vendor adapter key (e.g. `openai`, `anthropic`, `google`). Must match a key returned by `build_vendors`. |
| `model` | `str` | (required) | Vendor model identifier (e.g. `gpt-4o-mini`). |
| `weight` | `NonNegativeFloat` | (required) | Base routing weight. Multiplied by `health_score × budget_score` to produce `effective_weight`. 0 is legal (candidate dormant). |

### `PriceEntry`

`gateway/models.py:63-67`. USD per 1M tokens for one `(provider, model)`.

| Field | Type | Default | Description |
|---|---|---|---|
| `input` | `NonNegativeFloat` | (required) | USD per 1M input tokens. |
| `output` | `NonNegativeFloat` | (required) | USD per 1M output tokens. |

Consumed by `Router` (or accounting helpers) to compute `cost_usd` on each `AttemptRecord`.

### `RateLimitEntry`

`gateway/models.py:70-74`. Fleet-wide RPM/TPM caps. Note: **both fields are `PositiveInt`** — zero is rejected.

| Field | Type | Default | Description |
|---|---|---|---|
| `rpm` | `PositiveInt` | (required) | Requests per minute. Enforced atomically per replica via Redis `RedisTokenBucket`. |
| `tpm` | `PositiveInt` | (required) | Tokens per minute. Enforced in the same Lua transaction as `rpm`. |

### `CallerEntry`

`gateway/models.py:77-83`. One authorized internal backend.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `CallerName` | (required) | Stable caller identifier; constrained by the `Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]` alias at `models.py:31` (regex constraint added in commit `0f86fe8`). Used as a Prometheus label (`gateway_requests_total{caller=...}`) and a Postgres FK. |
| `key_hash` | `str` | (required) | Free-form string. In practice prefixed `sha256:<hex>`; the auth module computes the hash before lookup. |
| `daily_token_cap` | `NonNegativeInt \| None` | `None` | Optional daily total-token cap. Enforced in `chat_completions` via `db.caller_tokens_used_today` (`gateway/app.py:346-357`). `None` means no cap. |
| `enabled` | `bool` | `True` | Soft disable flag. Disabled callers cannot authenticate. |

### `RoutingConfig`

`gateway/models.py:86-91`. Tuning knobs for the routing subsystem.

| Field | Type | Default | Description | Consumer |
|---|---|---|---|---|
| `refresh_interval_ms` | `PositiveInt` | `1000` | Interval between `RefreshTask.tick()` calls. Lower → fresher weights, more Redis traffic. | `routing/refresh.py` |
| `health_window_s` | `PositiveInt` | `60` | Sliding-window length for breaker/observation aggregation. | `routing/observe.py`, `breaker.py` |
| `target_latency_s` | `float > 0` | `3.0` | Reference latency in `health_score = target/(target+observed)`. Lower → more aggressive penalty for slow vendors. | `routing/weights.py` |
| `min_weight_floor` | `NonNegativeFloat` | `0.02` | Effective weights below the floor are clamped to 0 (candidate excluded from selection). | `routing/weights.py` |
| `rng_seed_env` | `str \| None` | `None` | Name of an env var whose integer value seeds the router RNG. If unset or empty, `random.SystemRandom()` is used (`gateway/app.py:171-176`). Used in tests and recorded replays. | `gateway/app.py` |

---

## Wire models (OpenAI-compatible)

These are what callers send and receive. They are the only types serialized to/from HTTP bodies on `/v1/chat/completions`.

### `Message` (`models.py:129-131`)

| Field | Type | Default | Description |
|---|---|---|---|
| `role` | `Literal["system","user","assistant","tool"]` | (required) | Standard OpenAI role. |
| `content` | `str` (`max_length=200_000`) | (required) | Per-message cap. Aggregate cap is checked separately (see below). |

### `ChatCompletionRequest` (`models.py:146-193`)

| Field | Type | Default | Description | Validation |
|---|---|---|---|---|
| `model` | `str` | (required) | **Logical tier name**, not a vendor model. Resolved against `Config.tiers`. |
| `messages` | `list[Message]` | (required) | `min_length=1, max_length=512` (`models.py:150`). |
| `max_tokens` | `int` | `1024` | `gt=0, le=16384` (`models.py:151`). Forwarded to vendors as the generation cap. |
| `temperature` | `float \| None` | `None` | `ge=0.0, le=2.0`. |
| `top_p` | `float \| None` | `None` | `ge=0.0, le=1.0`. |
| `stream` | `bool` | `False` | Rejected if `True` — see `_no_streaming_in_v1` (`models.py:157-162`). |
| `metadata` | `dict[str,str] \| None` | `None` | Custom `field_validator` (`models.py:164-184`): at most 16 entries; each key ≤ 64 chars; each value ≤ 256 chars. |

Two model-level validators:

- `_no_streaming_in_v1` — rejects `stream=true` (`models.py:157-162`).
- `_validate_aggregate_content_size` — sums `len(m.content)` across messages and rejects ≥ 1,000,000 chars (`models.py:186-193`).

The per-message, per-list, and aggregate caps together bound a single request's worst-case memory + token cost; they were added in commit `0f86fe8` and close cr-1 §4.1 / §4.3.

### `ChatCompletionResponse` (`models.py:196-201`)

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | `str` | (required) | Gateway-issued request id. |
| `object` | `Literal["chat.completion"]` | `"chat.completion"` | OpenAI compat constant. |
| `model` | `str` | (required) | Echoed back as the logical tier the caller asked for. |
| `choices` | `list[Choice]` | (required) | Normally length 1. |
| `usage` | `Usage` | (required) | Token counts, summed across attempts? No — only the successful attempt's counts; see `data-plane.md`. |

### `Choice` (`models.py:140-143`)

| Field | Type | Default | Description |
|---|---|---|---|
| `index` | `NonNegativeInt` | (required) | Choice position. |
| `message` | `Message` | (required) | Assistant reply. |
| `finish_reason` | `str \| None` | `None` | Vendor-normalized (e.g. `stop`, `length`, `content_filter`). |

### `Usage` (`models.py:134-137`)

`prompt_tokens`, `completion_tokens`, `total_tokens` — all `NonNegativeInt`. No cross-field validator: the gateway trusts the vendor's accounting.

### `ErrorBody` (`models.py:204-207`)

The shape returned to callers in 4xx/5xx bodies (used by both `router.RouterError.body` and `errors.caller_error_for`).

| Field | Type | Default |
|---|---|---|
| `type` | `str` | (required) |
| `message` | `str` | (required) |
| `retryable` | `bool` | `False` |

---

## Internal DTOs

These never cross an HTTP boundary; they are the gateway's internal vocabulary.

### `ChatParams` (`models.py:213-218`)

Generation params handed from `Router` to a `Vendor.chat()` call.

| Field | Type | Default |
|---|---|---|
| `max_tokens` | `PositiveInt` | (required) |
| `temperature` | `float \| None` | `None` |
| `top_p` | `float \| None` | `None` |

### `ChatResult` (`models.py:221-228`)

Normalized vendor response. The vendor adapter is responsible for filling these fields regardless of upstream shape.

| Field | Type | Default | Description |
|---|---|---|---|
| `text` | `str` | (required) | Single assistant text. |
| `finish_reason` | `str \| None` | `None` | Normalized stop reason. |
| `input_tokens` | `NonNegativeInt` | (required) | From vendor accounting if present, else estimated. |
| `output_tokens` | `NonNegativeInt` | (required) | Same. |
| `vendor_request_id` | `str \| None` | `None` | Whatever request id the vendor surfaces; logged + persisted, never returned to caller. |

### `CandidateRef` (`models.py:231-240`)

Hashable `(provider, model)` handle. `frozen=True` so it can be a `dict` / `set` key. `.key()` returns `"<provider>/<model>"`, the same string used as the price/limit map key.

### `AttemptRecord` (`models.py:243-258`)

One row in the Postgres `requests` table — **every attempt is recorded, not just the winner**.

| Field | Type | Notes |
|---|---|---|
| `request_id` | `str` | Gateway-issued. |
| `caller` | `CallerName` | Validated to the same regex. |
| `tier` | `str` | Logical tier. |
| `provider`, `model` | `str` | Actual upstream. |
| `attempt_idx` | `NonNegativeInt` | 0 for first attempt. |
| `input_tokens`, `output_tokens` | `NonNegativeInt` | Default `0` (set non-zero on `ok`). |
| `cost_usd` | `NonNegativeFloat` | Default `0.0`. |
| `latency_ms` | `NonNegativeInt` | Per-attempt wall time. |
| `status` | `str` | `"ok"` or a `ProviderErrorKind.value`. |
| `vendor_req_id` | `str \| None` | Logged. |
| `client_trace_id` | `str \| None` (`max_length=128`) | Optional caller-supplied trace id. |

### `Caller` (`models.py:261-266`)

Identity + policy hydrated by `CallerResolver` from the Postgres `callers` table.

| Field | Type | Default |
|---|---|---|
| `name` | `CallerName` | (required) |
| `daily_token_cap` | `NonNegativeInt \| None` | `None` |
| `enabled` | `bool` | `True` |

### `ProviderErrorKind` (`models.py:37-45`)

Normalized vendor error taxonomy. See [`observability.md`](observability.md#error-taxonomy) for full mapping.

Values: `rate_limited`, `transient_5xx`, `timeout`, `bad_request`, `auth`, `content_filtered`.

---

## Cross-validation rules

`Config._cross_validate_candidates_have_pricing_and_limits` (`gateway/models.py:107-120`) runs after field validation. For each `(tier_name, candidate)` it asserts:

```
f"{cand.provider}/{cand.model}" in self.prices       — else ValueError
f"{cand.provider}/{cand.model}" in self.rate_limits  — else ValueError
```

This is the only structural cross-check. It prevents the most common operator mistake (adding a tier candidate without giving the router a way to price or rate-limit it).

What is **not** cross-checked:

- That `prices` and `rate_limits` keys are syntactically `"<provider>/<model>"`. They are dict keys, so any string is accepted.
- That a `prices`/`rate_limits` entry has a matching tier candidate. Dead entries are silently ignored.
- That `provider` strings match an actual vendor adapter. That check happens at `build_vendors` time (`gateway/providers/__init__.py`), and `RefreshTask` filters tiers down to `available_providers` (`gateway/app.py:166`).
- That `tier` names are non-empty or follow a naming rule. Callers' `model` field is matched literally.

---

## Loader + reload

### `load_config(path)` — `config/__init__.py:23-26`

```python
raw = Path(path).read_text()
data = yaml.safe_load(raw)
return Config.model_validate(data)
```

A thin wrapper. There is no schema migration, no defaulting beyond what Pydantic does, and no preprocessing. YAML errors surface as `yaml.YAMLError`; structural errors as `pydantic.ValidationError`.

### `ConfigHolder` — `config/__init__.py:29-46`

```python
class ConfigHolder:
    def __init__(self, value: Config, source_path: str | None = None) -> None:
        self.value = value
        self.source_path = source_path

    def reload(self) -> None:
        if not self.source_path:
            log.warning("ConfigHolder.reload called but no source_path is set")
            return
        try:
            new = load_config(self.source_path)
            self.value = new
            log.info("config reloaded from %s", self.source_path)
        except Exception:
            log.exception("config reload from %s failed; keeping old config", ...)
```

Semantics:

- **Read pattern.** Consumers pin a reference to the *holder* (e.g. `app.state.cfg = holder` at `gateway/app.py:191`) and read `holder.value` on demand. Anything that captures `holder.value` at boot keeps a stale copy.
- **Atomic swap.** The replacement `self.value = new` is a single Python attribute assignment — atomic with respect to the GIL. There is no read lock; concurrent readers either see the old or new `Config` whole, never a half-built one.
- **Validation-failure fallback.** If `load_config` raises (YAML parse error, `ValidationError`, missing file), the exception is logged with traceback and the old `Config` remains in place. The process keeps serving.

### `install_sighup_reload(holder)` — `config/__init__.py:49-57`

Registers a one-line handler that calls `holder.reload()`. Wrapped in `try/except (AttributeError, ValueError)` so it no-ops on:

- Platforms without `signal.SIGHUP` (Windows).
- Code paths where the handler is being registered from a non-main thread (tests, embedded use).

### What SIGHUP **does not** rebuild

Reloading swaps only `Config`. Everything else built from `Config` at boot keeps running with its old view:

| Subsystem | Built from | Reloaded by SIGHUP? |
|---|---|---|
| `vendors = build_vendors(cfg, secrets)` | `gateway/app.py:154` | **No.** Vendor SDK clients (`openai.AsyncOpenAI`, etc.) remain. New tiers' vendors won't exist until restart. |
| `RedisTokenBucket(limits=cfg.rate_limits)` | `gateway/app.py:156` | **No.** Holds a `dict` snapshot of `cfg.rate_limits` at construction. |
| `Observer(window_s=cfg.routing.health_window_s)` | `gateway/app.py:158` | **No.** Window length is captured at init. |
| `WeightEngine(routing=cfg.routing)` | `gateway/app.py:159` | **No.** `target_latency_s`, `min_weight_floor` are captured. |
| `RefreshTask(config=cfg, ...)` | `gateway/app.py:160-167` | **No.** Iterates over `cfg.tiers` it captured at boot. |
| `Router(config=cfg, ...)` | `gateway/app.py:181-188` | **No.** Same. |
| RNG | `gateway/app.py:171-176` | **No.** Seeded once. |
| Redis client, DB pool, secrets manager, accounting queue | lifespan body | **No.** |

**Practical consequence:** SIGHUP is useful for `callers` changes (the holder's `cfg.callers` is consulted at auth time in some code paths) but **adding a tier, changing a price, or changing a rate limit requires a process restart**. Operators should not rely on SIGHUP for routing changes.

---

## Env-var overrides

The lifespan reads several env vars before constructing dependencies. None are owned by `Config` itself — they are read in `gateway/app.py`. Listed here so operators have one reference:

| Env var | Default | Purpose | Read at |
|---|---|---|---|
| `GATEWAY_CONFIG` | `"config.yaml"` | Path to the YAML to load (and re-load on SIGHUP). | `app.py:94` |
| `GATEWAY_PROVIDER_MODE` | (none — file wins) | Overrides `Config.provider_mode`. Re-validated through `Config.model_validate` so a typo (`reel`) fails at boot, not at first vendor call. | `app.py:69-87, 98` |
| `GATEWAY_SECRETS_MODE` | (none — file wins) | Overrides `Config.secrets_mode`. Same re-validation. | `app.py:69-87, 99` |
| `GATEWAY_DB_DSN` | dev: `postgres://gateway:gateway@localhost:5432/gateway`; **required** when `provider_mode=real` | Postgres connection string. Missing-in-real fails loudly (`RuntimeError`) per cr-1 §2.3. | `app.py:109-116` |
| `GATEWAY_REDIS_URL` | `"redis://localhost:6379/0"` | Redis URL. | `app.py:104` |
| `GATEWAY_LOG_LEVEL` | `"INFO"` | Passed to `configure_logging`. Below INFO disables log redaction (`gateway/logging.py:114`). | `app.py:92` |
| `GATEWAY_METRICS_TOKEN` | (none — fail-closed) | Bearer token compared (constant-time) against the `Authorization` header on `/metrics`. Loaded into the `SecretsManager` in mock mode (`app.py:144-152`); read directly in env mode. | `app.py:66, 257-270` |
| Value of `cfg.routing.rng_seed_env` (e.g. `GATEWAY_RNG_SEED`) | (none → `SystemRandom`) | If set, used to seed `random.Random` for deterministic routing. | `app.py:171-176` |

The override path (`_apply_env_overrides`, `app.py:69-87`) `model_dump`s the parsed `Config`, applies the override, and re-validates. This is the **only** place `provider_mode` / `secrets_mode` can differ from the YAML — `ConfigHolder.reload()` does **not** re-apply env overrides, so a SIGHUP after an env override will revert to the YAML's `provider_mode` / `secrets_mode` until the next process start.

---

## Worked example: `config.yaml`

Walk through `/home/prnsh/gh/or/config.yaml` and what each section drives.

```yaml
provider_mode: real
secrets_mode: env
```

`build_vendors(cfg, secrets)` returns the real SDK adapters; `build_secrets_manager("env")` returns the `os.environ`-backed manager. Both decisions are made once at boot (`gateway/app.py:135, 154`).

```yaml
tiers:
  fast:
    - { provider: anthropic, model: claude-haiku-4-5,  weight: 50 }
    - { provider: openai,    model: gpt-4o-mini,       weight: 30 }
    - { provider: google,    model: gemini-2.5-flash,  weight: 20 }
```

Three candidates available when a caller posts `model: "fast"`. Their `weight` (50, 30, 20) is the **base** that gets multiplied by `health_score × budget_score` each refresh tick. The router never sees raw `weight` — only the post-multiplied `effective_weight` in `WeightEngine`.

```yaml
routing:
  refresh_interval_ms: 1000
  ...
  rng_seed_env: GATEWAY_RNG_SEED
```

Drives `RefreshTask` cadence and `WeightEngine` math. Reading `rng_seed_env` is a two-step indirection: this YAML names the env var, `gateway/app.py:171-176` reads `os.environ[that_name]`.

```yaml
prices:
  anthropic/claude-haiku-4-5:  { input: 1.00, output: 5.00 }
```

USD per 1M tokens. Read once at boot by whatever code computes `cost_usd` for each `AttemptRecord`. **Not** re-read after SIGHUP (the `Router` captured `cfg`).

```yaml
rate_limits:
  anthropic/claude-haiku-4-5:  { rpm: 4000, tpm: 400000 }
```

Snapshot copied into `RedisTokenBucket.limits` at construction. The bucket key in Redis encodes the cap, so changing the cap here doesn't propagate to Redis until restart.

```yaml
callers:
  - { name: search-svc, key_hash: "sha256:...", daily_token_cap: 10000000 }
```

Static caller registry. Authoritative for `daily_token_cap` and `enabled`; `CallerResolver` queries Postgres `callers` at request time but uses the same shape. Maintaining both is intentional: YAML is the operator-facing source of truth; Postgres is what the hot path queries.

`config.dev.yaml` differs in three ways:

- `provider_mode: mock`, `secrets_mode: mock`.
- Tighter `rate_limits` (1000 rpm rather than 4000 — useful for triggering breakers in dev).
- `callers: []` — empty list. In dev, all callers come from Postgres seeded by `./scripts/setup.sh`.

---

## Concurrency model

- `load_config` is sync I/O — called only at boot and from the SIGHUP handler. SIGHUP delivery is on the main thread (Python default), so the handler is not concurrent with other request-serving threads, but it **is** concurrent with the asyncio event loop. Because the swap is one assignment, this is safe.
- `ConfigHolder` is not thread-safe in any formal sense — it relies on CPython attribute-assignment atomicity. Two simultaneous SIGHUP-triggered reloads would race the disk read, not the assignment.
- All Pydantic models in `models.py` are **immutable in spirit** (Pydantic v2 default is mutable, but consumers do not mutate). `CandidateRef` is `frozen=True` (`models.py:234`) because it is used as a dict/set key.

## Failure modes

| Trigger | Where | Effect |
|---|---|---|
| YAML parse error at boot | `load_config` | `yaml.YAMLError` → uvicorn fails to start lifespan. |
| Pydantic validation error at boot | `load_config` → `Config.model_validate` | `ValidationError` with all field errors → uvicorn fails to start lifespan. |
| Cross-validation: tier candidate lacks a price/limit | `Config._cross_validate_candidates_have_pricing_and_limits` | `ValueError` with the offending `tier/provider/model`. |
| Env override invalid (`GATEWAY_PROVIDER_MODE=reel`) | `_apply_env_overrides` re-validates | `ValidationError` at boot, not at first vendor call (intentional — see app.py docstring). |
| YAML edited to invalid form, SIGHUP delivered | `ConfigHolder.reload()` | Exception is logged with traceback; **old `Config` kept**; process continues. |
| SIGHUP delivered on a non-main thread or non-POSIX platform | `install_sighup_reload` | Handler registration silently no-ops. |
| Caller pushes `Authorization: Bearer ...` to `/metrics` without `GATEWAY_METRICS_TOKEN` set | `app.py:263-264` | Returns 401 fail-closed. |

## Configuration knobs

This module *is* the configuration knob. Operators turn `Config` fields by editing the YAML; runtime tuning is just `kill -HUP <pid>` modulo the swap caveats above.

## Open questions / known gaps

- **Dev key in repo.** `config.dev.yaml` previously shipped with a SHA-256-matched dev key. See [`cr-1.md` §2.1](../../code-review/cr-1.md). The config schema does not (and cannot) prevent committing real-looking key hashes.
- **cr-1 §4.1 (no `max_tokens` cap, no per-request size cap).** Resolved in commit `0f86fe8` — `max_tokens` is now bounded by `le=16384`, message content by `max_length=200_000`, message list by `max_length=512`, and aggregate content by the `_validate_aggregate_content_size` model-validator (1,000,000 chars).
- **cr-1 §4.3 (unbounded `metadata`).** Resolved in commit `0f86fe8` — `_validate_metadata_bounds` caps entries at 16, key length at 64 chars, value length at 256 chars.
- **cr-1 §4.4 (`CallerEntry.name` regex).** Resolved in commit `0f86fe8` — `CallerName` now applies `Field(pattern=r"^[a-z0-9_-]{1,64}$")` at the type level. Prefix-reservation (separating tenant-allocated names from reserved labels like `admin` / `ops`) remains unsolved; the regex still permits them.
- **SIGHUP trust on file ACLs.** [`cr-1.md` §9.1](../../code-review/cr-1.md). SIGHUP rereads the on-disk file — anyone who can edit it and signal the process can swap caller hashes. No signature, no checksum, no read-only mount required.
- **SIGHUP only swaps `Config`.** Most downstream subsystems captured `cfg` at boot (table above). Adding a tier, changing a price, or changing a rate limit is *not* a hot operation — restart required.
- **No `Config.model_config` `frozen=True`.** `Config` is mutable; nothing prevents test code (or buggy production code) from mutating `holder.value.tiers` in place. Convention is read-only.
