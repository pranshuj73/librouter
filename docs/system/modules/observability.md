# Observability

`gateway/logging.py` + `gateway/metrics.py` + `gateway/errors.py`

## Purpose

The three streams the gateway exposes to operators:

1. **Structured JSON logs** — one event per line on stdout, ISO timestamps, configurable redaction.
2. **Prometheus metrics** — request volume, attempt outcomes, routing weights, breaker state, bucket fill, USD cost, accounting backpressure.
3. **Postgres `requests` audit log** — every attempt persisted via `AccountingQueue`.

This module documents the first two end-to-end and the **error taxonomy** that maps vendor failures into both. The Postgres audit log is documented in [`modules/accounting.md`](accounting.md) and [`modules/db.md`](db.md); cross-references below show how it ties in.

## Public surface

| Symbol | File | Kind | Notes |
|---|---|---|---|
| `configure_logging(level=None)` | `logging.py:86` | function | Idempotent; called once at lifespan entry. |
| `get_logger(name=None)` | `logging.py:132` | function | Thin wrapper over `structlog.get_logger`. |
| `RedactProcessor` | `logging.py:31` | structlog processor | Inserted into the pipeline unless level is DEBUG. |
| `REGISTRY` | `metrics.py:15` | `CollectorRegistry` | Module-local registry. |
| `REQUESTS_TOTAL`, `REQUEST_LATENCY`, `ATTEMPTS_TOTAL`, `ROUTING_WEIGHT`, `BREAKER_STATE`, `BUCKET_REMAINING`, `COST_USD_TOTAL`, `ACCOUNTING_DROPPED`, `REDIS_DOWN`, `REFRESH_ERRORS_TOTAL` | `metrics.py:17-83` | Prometheus collectors | See catalog below. |
| `render_metrics()` | `metrics.py:86-87` | function | Returns `(body, content_type)` for `/metrics`. |
| `ProviderError` (+ 6 subclasses) | `errors.py:19-62` | exception types | Vendor-error taxonomy. |
| `RETRYABLE_ERROR_TYPES`, `NON_RETRYABLE_ERROR_TYPES` | `errors.py:65-75` | tuples | Used by `Router` to gate retry vs. fail-fast. |
| `caller_error_for(exc)` | `errors.py:85-110` | function | `ProviderError` → `(http_status, ErrorBody)` with **canonical** caller-safe text. |

---

## Logging

### Pipeline

`configure_logging` (`gateway/logging.py:86-129`) builds a structlog processor chain:

| # | Processor | Purpose |
|---|---|---|
| 1 | `structlog.contextvars.merge_contextvars` | Merge `request_id`, `caller`, etc. bound via `bind_contextvars`. |
| 2 | `structlog.processors.add_log_level` | Add `level` key. |
| 3 | `structlog.processors.TimeStamper(fmt="iso")` | Add `timestamp` in ISO-8601. |
| 4 | `RedactProcessor(...)` | Optional. Inserted only when **level > DEBUG** *and* `redact` block exists in `logger.json` (`logging.py:114`). |
| 5 | `structlog.processors.JSONRenderer` | Final renderer — produces one JSON object per line. |

`wrapper_class` is a filtering bound logger (`logging.py:127`): events below the threshold are dropped *before* formatting, so DEBUG events have zero cost in production.

### Level resolution

`configure_logging(level)` resolves in priority order (`logging.py:88-97`):

1. The `level` argument passed by `app.py` from `GATEWAY_LOG_LEVEL` (`app.py:92`).
2. The `level` key in `gateway/config/logger.json`.
3. Hard-coded `"INFO"`.

`logging.basicConfig` is also called so stdlib loggers (`asyncpg`, `httpx`) route through the same stream.

### Redaction — `RedactProcessor`

`gateway/logging.py:31-83` is a structlog processor inserted into the pipeline by `configure_logging`. Its configuration lives entirely in `gateway/config/logger.json` — the file is read on every call to `configure_logging` via `_load_logger_config()` (`logging.py:23-28`), which returns `{}` on missing-or-malformed-JSON without raising. The full file shape:

```json
{
  "level": "INFO",
  "redact": {
    "patterns":   ["(?i)bearer\\s+[A-Za-z0-9._\\-]+","sk-[A-Za-z0-9]{16,}","ant-[A-Za-z0-9]{16,}","gsk-[A-Za-z0-9]{16,}","AIza[A-Za-z0-9_\\-]{20,}"],
    "field_names":["authorization","api_key","api-key","token","secret","password","key_hash"],
    "replacement":"<redacted>"
  }
}
```

The constructor compiles every `patterns` entry to a `re.Pattern` once and folds `field_names` into a `frozenset` lower-cased for O(1) match (`logging.py:51-52`). Algorithm (`logging.py:63-72`), executed by `__call__` over the event dict:

1. If `key.lower()` is in `field_names`, return the `replacement` token wholesale — no inspection of the value.
2. Else if the value is a `str`, run each compiled regex as `pattern.sub(replacement, value)` in order. The order matters only insofar as the first-matching substring is rewritten and subsequent patterns scan the rewritten string.
3. Else if the value is a `dict`, recurse one level — each `(k, v)` pair is fed back through `_scrub_value`. The recursion is hard-capped at one level (no second recursion is performed even if the nested dict contains another dict).
4. Else (`int`, `bool`, `None`, `list`, `frozenset`, `tuple`, custom objects) leave the value alone.

Implication: **list-valued fields are not scrubbed**. If a future log site emits `headers=["Authorization: Bearer ..."]`, the token would pass through. Today no such site exists.

### DEBUG-level bypass

`logging.py:114`: `if effective_level > logging.DEBUG and redact_cfg:` — `RedactProcessor` is appended to the structlog processor chain **only** when both conditions hold:

1. The effective level resolves strictly above `DEBUG` (i.e. `INFO`, `WARNING`, `ERROR`, `CRITICAL`).
2. The `redact` block in `logger.json` is non-empty.

So `GATEWAY_LOG_LEVEL=DEBUG` (or `level: "DEBUG"` in `logger.json`) silently turns redaction off so triagers see raw upstream payloads — a deliberate trade-off worth flagging on any incident where DEBUG was enabled for an extended window. A missing or empty `redact` block also disables redaction independently of level.

### Example output shape

A successful request from the chat completions handler produces (roughly):

```json
{"level":"info","timestamp":"2026-05-31T15:42:01.213Z","event":"chat_completed","request_id":"r_01HX...","caller":"chat-svc","tier":"fast","provider":"openai","model":"gpt-4o-mini","latency_ms":612}
```

(`event` is the positional message passed to the bound logger; other keys come from `bind_contextvars` or kwargs.)

A redacted line where the bound logger emitted a stringified header dict:

```json
{"level":"warning","timestamp":"2026-05-31T15:42:01.555Z","event":"upstream_5xx","authorization":"<redacted>","headers":{"x-request-id":"abc","authorization":"<redacted>"},"detail":"Bearer <redacted>"}
```

Three redactions happened: the top-level `authorization` key (field match), the nested `authorization` inside `headers` (one-level recursion), and the `Bearer …` substring inside `detail` (pattern match).

### `logger.json` reference

| Key | Type | Default if absent | Effect |
|---|---|---|---|
| `level` | string | `"INFO"` | Fallback log level (env var still wins). |
| `redact.patterns` | list[str] | `[]` | Regex patterns substituted in any string value. Compiled once. |
| `redact.field_names` | list[str] | `[]` | Keys whose values are replaced wholesale (case-insensitive). |
| `redact.replacement` | string | `"<redacted>"` | The replacement token. |

If the file is missing or malformed (`_load_logger_config` returns `{}`), there is **no redaction** at all — only the level fallback chain applies.

### Open log gaps

- **No PII / message-content redaction.** [`cr-1.md` §8.1](../../code-review/cr-1.md). The redactor strips secrets, not user content. If a vendor adapter logs `messages=[...]` for debugging, end-user prompts will land in stdout.
- **No `caller_id` enforcement.** Nothing in the pipeline asserts that every log line tied to a request includes the caller. Contextvars are advisory.
- **Vendor `vendor_detail` strings flow into logs verbatim.** See [Error taxonomy](#error-taxonomy) and [`cr-1.md` §4.2](../../code-review/cr-1.md). They may include upstream IDs or fragments.

---

## Metrics

All collectors are registered against a module-local `REGISTRY` (`metrics.py:15`) — **not** the prometheus_client global. Tests can therefore construct a fresh app without colliding with prior runs.

`render_metrics()` (`metrics.py:80-81`) returns `(generate_latest(REGISTRY), CONTENT_TYPE_LATEST)`. It is called by the `/metrics` endpoint after `_refresh_observability_gauges` rebuilds the per-candidate gauges (`gateway/app.py:272-273`). The gauges are therefore **scrape-time computed**, not continuously updated.

### Collector catalog

| Name | Type | Labels | What it represents | Emitted at |
|---|---|---|---|---|
| `gateway_requests_total` (`REQUESTS_TOTAL`) | Counter | `caller, tier, outcome` | Total `/v1/chat/completions` requests. `outcome` is `"ok"` or a `RouterErrorKind.value` (`upstream_unavailable`, `deadline_exceeded`, `invalid_request`, `auth`). | `app.py:368` (error path), `app.py:376` (success path). |
| `gateway_request_latency_seconds` (`REQUEST_LATENCY`) | Histogram | `tier, outcome` | End-to-end wall time. Buckets: `0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0` (`metrics.py:28`). | `app.py:371, 379`. |
| `gateway_attempts_total` (`ATTEMPTS_TOTAL`) | Counter | `provider, model, status` | Total vendor calls. `status` is `"ok"` or `ProviderErrorKind.value` (`rate_limited`, `transient_5xx`, `timeout`, `bad_request`, `auth`, `content_filtered`). One increment per `AttemptRecord` in the returned `ChatResult`. | `app.py:382-384`. |
| `gateway_routing_weight` (`ROUTING_WEIGHT`) | Gauge | `provider, model` | Most recently *recomputed* effective routing weight (post-`min_weight_floor`). | `app.py:306`, computed from `WeightEngine.signals_for` at scrape time. |
| `gateway_breaker_state` (`BREAKER_STATE`) | Gauge | `provider, model` | `0 = CLOSED, 1 = HALF_OPEN, 2 = OPEN`. | `app.py:307-313`, read from `engine.signals_for(...).breaker`. |
| `gateway_bucket_remaining` (`BUCKET_REMAINING`) | Gauge | `provider, model, dim` | Remaining capacity in the Redis token bucket. `dim ∈ {rpm, tpm}`. | `app.py:314-319`. |
| `gateway_cost_usd_total` (`COST_USD_TOTAL`) | Counter | `caller, tier, provider` | Cumulative USD attributed to successful attempts. Sum of `AttemptRecord.cost_usd` per `(caller, tier, provider)`. | `app.py:386-388` (`status=="ok"` only). |
| `gateway_accounting_dropped_total` (`ACCOUNTING_DROPPED`) | Counter | (none) | Rows lost by the accounting queue. Live-emitted on **writer-exception** (`AccountingQueue._flush` catches `Database.write_batch` failures and `inc(n)`s by the batch size) per cr-1 §6.3 fix in commit `8e046e5`. Overflow eviction in `enqueue` (capacity-bounded `deque`, `accounting.py:52-59`) bumps `self._dropped_total` but currently does **not** call `ACCOUNTING_DROPPED.inc()`; only the writer-exception path live-emits. | `accounting.py:111`. |
| `gateway_redis_down` (`REDIS_DOWN`) | Gauge | (none) | `1` if Redis is currently believed unreachable, else `0`. Set to `0` once at boot (`app.py:121`). **Never updated on subsequent failure** — see open gap. | `app.py:121` only. |
| `gateway_refresh_errors_total` (`REFRESH_ERRORS_TOTAL`) | Counter | (none) | Number of `RefreshTask` ticks that raised. Incremented in the `_loop` exception path of `gateway/routing/refresh.py:129` once per failed tick (commit `ea5b60c`). Pairs with the jittered exponential backoff so the counter grows linearly even though log spam is suppressed after the first failure of a run. Useful as an alerting signal for "background reconciler is degraded" — see [`routing.md`](routing.md). | `gateway/routing/refresh.py:129`. |

### Update cadence

| Collector | Update cadence |
|---|---|
| `REQUESTS_TOTAL`, `REQUEST_LATENCY` | Per request, in the chat handler. |
| `ATTEMPTS_TOTAL`, `COST_USD_TOTAL` | Per attempt, in the chat handler's `for a in result.attempts` loop. |
| `ROUTING_WEIGHT`, `BREAKER_STATE`, `BUCKET_REMAINING` | **At scrape time**, by `_refresh_observability_gauges` (`app.py:272, 277-319`). Between scrapes the values are stale. |
| `ACCOUNTING_DROPPED` | Per failed batch flush in `AccountingQueue._flush` (live). Not currently emitted on capacity-overflow eviction in `enqueue`. |
| `REDIS_DOWN` | Once, at boot. Stuck at `0` thereafter. |
| `REFRESH_ERRORS_TOTAL` | Per failed `RefreshTask.tick`, in the `_loop` exception handler (live). |

### Outcome → status mapping

`REQUESTS_TOTAL{outcome=…}` uses `RouterErrorKind.value` directly. The full set:

| `outcome` | Meaning | HTTP returned to caller |
|---|---|---|
| `ok` | Router returned a `ChatResult`. | 200 |
| `invalid_request` | `RouterErrorKind.INVALID_REQUEST` — body failed validation downstream of FastAPI's parse. | 400 |
| `auth` | `RouterErrorKind.AUTH` — the only non-retryable auth path that reaches this metric. Caller-level 401s from bearer rejection do **not** increment this counter. | 401 |
| `upstream_unavailable` | `RouterErrorKind.UPSTREAM_UNAVAILABLE` — all candidates excluded with budget remaining. | 503 |
| `deadline_exceeded` | `RouterErrorKind.DEADLINE_EXCEEDED` — `total_budget_s` drained mid-failover. | 504 |

`ATTEMPTS_TOTAL{status=…}` uses `"ok"` or any of the six `ProviderErrorKind.value`s. Both label sets share `auth` and `bad_request` strings — but the semantic is per-surface: at the *attempt* level it means "this vendor rejected this call"; at the *request* level it means "the router gave up".

### `/metrics` auth

`gateway/app.py:249-274`. Bearer-gated: `GATEWAY_METRICS_TOKEN` must exist in the `SecretsManager`, and the caller's `Authorization: Bearer …` is compared with `hmac.compare_digest`. Fail-closed (401) if either is missing. Was previously unauthenticated — see [`cr-1.md` §3.3](../../code-review/cr-1.md).

**Operator note.** Prometheus (or any scraper) must present `Authorization: Bearer <GATEWAY_METRICS_TOKEN>` on every scrape. A representative `scrape_configs` entry:

```yaml
- job_name: gateway
  authorization:
    type: Bearer
    credentials_file: /etc/prometheus/gateway-metrics-token
  static_configs:
    - targets: ["gateway:8080"]
```

Without the header the scrape returns 401 and the target appears `DOWN` in Prometheus, so dashboards rely on the token being mounted into the scraper before the gateway is upgraded past this change.

---

## Error taxonomy

`gateway/errors.py` defines the **only** exception types `Router` will catch from a `Vendor`. Anything else propagates as a 500. The taxonomy doubles as the `ATTEMPTS_TOTAL{status=...}` label set.

### Vendor-text sanitization (cr-1 §4.2 — Resolved in commit `0323080`)

The module docstring states the contract bluntly (`errors.py:7-12`):

> ``caller_error_for`` produces fixed canonical strings in the caller-visible response body. Vendor SDK messages (which can contain upstream response bodies, request IDs, or key fragments) are kept in ``ProviderError.vendor_detail`` for structured-log use only; they never reach the caller.

`ProviderError.message` is therefore the canonical surface — vendor adapters must pass a short safe string (typically the exception class name) and put the raw SDK text on the keyword-only `vendor_detail` field. The two fields cannot collide because the constructor takes `vendor_detail` as a keyword-only argument:

```python
# errors.py:36-38
def __init__(self, message: str = "", *, vendor_detail: str | None = None) -> None:
    super().__init__(message or type(self).__name__)
    self.vendor_detail: str | None = vendor_detail
```

`caller_error_for` (`errors.py:85-110`) does **not** read `exc.args[0]` or `exc.vendor_detail`; it indexes into the static `_CALLER_MESSAGES` table (`errors.py:78-82`) so even an adapter that accidentally passes a raw SDK string as `message` cannot smuggle it past this boundary for the three non-retryable classes. The 500 fallthrough is a fixed `"upstream error"`.

### Hierarchy

```
ProviderError                 (errors.py:19)   default kind = TRANSIENT_5XX
├── RateLimited               (errors.py:41)   kind = RATE_LIMITED       — retryable
├── Transient5xx              (errors.py:45)   kind = TRANSIENT_5XX      — retryable
├── Timeout                   (errors.py:49)   kind = TIMEOUT            — retryable
├── BadRequest                (errors.py:53)   kind = BAD_REQUEST        — non-retryable
├── AuthError                 (errors.py:57)   kind = AUTH               — non-retryable
└── ContentFiltered           (errors.py:61)   kind = CONTENT_FILTERED   — non-retryable
```

`message` is the **safe public string** (defaults to the class name when omitted). `vendor_detail` is the raw upstream SDK string, kept for operator logs only — by contract, `caller_error_for` does not include it. See the construction snippet above.

### Retry classification

| Tuple | Members | Used by |
|---|---|---|
| `RETRYABLE_ERROR_TYPES` (`errors.py:65-69`) | `RateLimited`, `Transient5xx`, `Timeout` | `Router.route` — add candidate to `exclude` set, repick, continue. |
| `NON_RETRYABLE_ERROR_TYPES` (`errors.py:71-75`) | `BadRequest`, `AuthError`, `ContentFiltered` | `Router.route` — stop immediately, return caller error. |

`ProviderErrorKind` values are the on-the-wire form (used in `ATTEMPTS_TOTAL.status` labels and `AttemptRecord.status`).

### `caller_error_for(exc)` — `errors.py:85-110`

Maps a non-retryable `ProviderError` to an HTTP status + body. The message is always one of the canonical strings in `_CALLER_MESSAGES` (`errors.py:78-82`):

| Exception | HTTP | `ErrorBody.type` | `ErrorBody.message` | `retryable` |
|---|---|---|---|---|
| `BadRequest` | 400 | `"invalid_request"` | `"request rejected by upstream provider"` | `false` |
| `AuthError` | 401 | `"auth"` | `"authentication failed"` | `false` |
| `ContentFiltered` | 400 | `"content_filtered"` | `"content filtered by upstream provider"` | `false` |
| (anything else) | 500 | `"internal"` | `"upstream error"` | `false` |

The 500 fallthrough should not occur in practice — the router pre-filters retryables — but is there as a safety net.

**Vendor SDK strings never reach the caller.** They are stored on `exc.vendor_detail` and emitted in structured logs by adapters. cr-1 §4.2 (caller-body sanitization) is resolved by commit `0323080`; what remains is that vendor strings still flow verbatim into **logs** (no field-level scrub for `vendor_detail` because the field is not in `logger.json`'s `field_names`). Operators relying on log redaction should add `vendor_detail` to `field_names`, or rely on the existing pattern list to catch known token shapes.

---

## End-to-end traceability

The same request appears on three observability surfaces. The table below maps the request lifecycle to which surface records it.

| Lifecycle step | Logs | Metrics | DB (`requests` row) |
|---|---|---|---|
| Bearer accepted | (via `CallerResolver` log if any) | — | — |
| Bearer rejected | `auth` warn | `REQUESTS_TOTAL{outcome=auth}` (via 401 from handler — actually raised before metric increment; logs only) | — |
| Daily cap exceeded | warn in handler | none currently — `REQUESTS_TOTAL` is only incremented after `router.route`; the 429 path bypasses it. **Gap.** | — |
| Routing pick | — | `ROUTING_WEIGHT` (at scrape, reflects last refresh) | — |
| Bucket acquire success | — | `BUCKET_REMAINING` (scrape-time) | — |
| Bucket acquire fail (dry) | (router log) | — — bucket failures show up as a *missed* pick, not a counter | — |
| Attempt OK | adapter log | `ATTEMPTS_TOTAL{status=ok}`, `COST_USD_TOTAL` | one `requests` row, `status=ok` |
| Attempt retryable error | adapter log | `ATTEMPTS_TOTAL{status=rate_limited|transient_5xx|timeout}` | one `requests` row per failed attempt |
| Attempt non-retryable error | adapter log | `ATTEMPTS_TOTAL{status=bad_request|auth|content_filtered}` | one `requests` row |
| Request OK overall | `chat_completed` info | `REQUESTS_TOTAL{outcome=ok}`, `REQUEST_LATENCY` | rows for *all* attempts, not just the winner |
| Request DEADLINE_EXCEEDED | warn | `REQUESTS_TOTAL{outcome=deadline_exceeded}`, `REQUEST_LATENCY` | rows for each attempt up to the deadline |
| Request UPSTREAM_UNAVAILABLE | warn | `REQUESTS_TOTAL{outcome=upstream_unavailable}`, `REQUEST_LATENCY` | rows for each excluded attempt |
| Breaker transition | (routing log) | `BREAKER_STATE` (scrape-time) | — |
| Accounting flush success | debug | — | rows inserted in batch |
| Accounting flush failure | exception | `ACCOUNTING_DROPPED.inc(n)` | rows dropped |
| Redis unreachable | exception in caller | — — `REDIS_DOWN` never updates after boot | — |
| `RefreshTask` tick raised | `log.exception("refresh tick failed")` on first failure of a run only | `REFRESH_ERRORS_TOTAL.inc()` per failed tick | — |

**Practical consequence for incident triage:**

- **What was the request rate during the outage?** Prometheus (`REQUESTS_TOTAL`) — but `caller` is a label, so cardinality is bounded by the caller registry.
- **Which vendor failed?** Prometheus (`ATTEMPTS_TOTAL{status!="ok"}`).
- **What did the vendor actually say?** Logs (`vendor_detail` on the adapter log line) or DB row (`requests.vendor_req_id` for correlation with the vendor's own logs).
- **Did we drop billing rows?** Prometheus (`ACCOUNTING_DROPPED`).
- **Was Redis up?** **Not from `REDIS_DOWN`.** You need to grep logs for Redis exceptions or look at request-level 500s.

---

## Cross-references

- [`modules/router.md`](router.md) — emits `REQUESTS_TOTAL`, `REQUEST_LATENCY`, decides `outcome` via `RouterErrorKind`.
- [`modules/routing.md`](routing.md) — drives `ROUTING_WEIGHT`, `BREAKER_STATE`, `BUCKET_REMAINING` (computed at scrape time from `WeightEngine.signals_for`).
- [`modules/accounting.md`](accounting.md) — emits `ACCOUNTING_DROPPED`; persists every `AttemptRecord` to the Postgres `requests` table.
- [`modules/providers.md`](providers.md) — raises `ProviderError` subclasses; sets `vendor_detail` on the exception; logs the raw SDK string before raising.
- [`modules/app.md`](app.md) — `lifespan` calls `configure_logging`; the `/metrics` handler enforces bearer auth; the chat handler is where `REQUESTS_TOTAL`/`REQUEST_LATENCY`/`ATTEMPTS_TOTAL`/`COST_USD_TOTAL` are actually called.
- [`modules/config.md`](config.md) — `GATEWAY_LOG_LEVEL`, `GATEWAY_METRICS_TOKEN`, and the `logger.json` redact block.

---

## Concurrency model

- structlog is bound per logger and thread-safe. `bind_contextvars` is per-`contextvars.Context` and survives asyncio task boundaries.
- prometheus_client `Counter` / `Gauge` / `Histogram` use atomic increments and per-label `Lock` internally. Concurrent `inc()` from many tasks is safe.
- `_refresh_observability_gauges` runs synchronously in the `/metrics` request handler. If multiple scrapes overlap, both will recompute and write; the writes are idempotent.
- `configure_logging` is called exactly once at lifespan entry. Calling it twice with different levels would replace the structlog config — not actively guarded against.

## Failure modes

| Trigger | Effect |
|---|---|
| `logger.json` missing or malformed | `_load_logger_config` returns `{}` (`logging.py:23-28`); defaults apply; no redaction. |
| Operator runs with `GATEWAY_LOG_LEVEL=DEBUG` | Redaction silently disabled. |
| Vendor adapter logs a list of headers containing a token | Not redacted (only string/dict values are scrubbed). |
| `Database.write_batch` raises | `ACCOUNTING_DROPPED.inc(n)` immediately; rows are lost; the originating request still succeeded. |
| Redis becomes unreachable mid-flight | `REDIS_DOWN` does **not** update; per-attempt errors propagate as 500s; logs show the underlying `redis.exceptions.*`. |
| `/metrics` scrape arrives before `WeightEngine` has any signals | `signals_for(ref)` returns `None` and the per-candidate gauges are skipped for that candidate (`app.py:283-284`). |
| `GATEWAY_METRICS_TOKEN` unset | `/metrics` returns 401 fail-closed; Prometheus scrape fails. |

## Configuration knobs

| Knob | Where | Effect |
|---|---|---|
| `GATEWAY_LOG_LEVEL` env var | `app.py:92` → `configure_logging` | Sets log filter level. Below INFO disables redaction. |
| `level` in `logger.json` | `logging.py:94-96` | Secondary source for the level. |
| `redact.{patterns, field_names, replacement}` in `logger.json` | `logging.py:115-121` | Tune the redactor. |
| `GATEWAY_METRICS_TOKEN` env var (or secrets backend) | `app.py:66, 263-269` | Bearer for `/metrics`. |
| Histogram bucket boundaries for `REQUEST_LATENCY` | `metrics.py:28` | Hard-coded; change requires a code edit. |

## Open questions / known gaps

- **`/metrics` was previously unauthenticated.** [`cr-1.md` §3.3](../../code-review/cr-1.md). Currently bearer-gated; the gap is that the token is one global value with no rotation hook.
- **Caller-spoofable `request_id`.** [`cr-1.md` §3.5](../../code-review/cr-1.md). The router accepts `metadata.request_id` as the `request_id` recorded in logs and the DB. A caller can collide IDs across callers and pollute log searches.
- **cr-1 §4.2 caller-body sanitization — Resolved in commit `0323080`.** `caller_error_for` reads from a static `_CALLER_MESSAGES` table; vendor SDK text is held on `ProviderError.vendor_detail` and never returned to the caller. Residual gap: `vendor_detail` still reaches adapter logs verbatim — the `RedactProcessor` only matches known token patterns and is bypassed entirely at DEBUG. To redact, add `"vendor_detail"` to `logger.json`'s `field_names`.
- **cr-1 §6.3 live `ACCOUNTING_DROPPED` — Resolved in commit `8e046e5`.** `accounting._flush` now `inc()`s live on every writer-exception (`accounting.py:111`); the shutdown bump in lifespan was removed. Residual gap: capacity-overflow eviction in `enqueue` (`accounting.py:52-59`) still increments only `self._dropped_total`, not the Prometheus counter — operators see the loss only via `dropped_total` exposed by the queue, not on `/metrics`.
- **`REDIS_DOWN` never updates after boot.** No code path sets it back to `1` when Redis is unreachable. Operators cannot use it as a real signal.
- **Daily-cap 429 does not increment `REQUESTS_TOTAL`.** The early `raise HTTPException(429, ...)` in `chat_completions` (`app.py:349-357`) returns before the metric/latency block. The miss is small but real.
- **No tracing.** There are no OpenTelemetry spans; correlation across replicas relies on `request_id` and `vendor_req_id` in logs.
- **No log-line `event=` discipline.** Different modules emit different shapes. No central schema for log keys.
