# `gateway/providers/` — Vendor adapters

## Purpose

This package is the **single boundary** between the gateway's hot path and any third-party LLM SDK. The router (see [`router.md`](router.md)) holds a `dict[str, Vendor]` of singletons and only ever calls one method on them — `Vendor.chat(...)` — receiving back a `ChatResult` or one of six normalized `ProviderError` subclasses. No vendor-specific request shape, response shape, or exception type leaks past this layer.

The package is split into three concerns:

1. **The contract** — `gateway/providers/base.py` defines the `Vendor` ABC. Everything below conforms.
2. **Real adapters** — one module per vendor (`openai.py`, `anthropic.py`, `google.py`). Each wraps the vendor's async SDK and translates SDK exceptions into the [`ProviderError` taxonomy](observability.md#error-taxonomy) declared in `gateway/errors.py`.
3. **Mock adapters** — `gateway/providers/mock/` holds programmable test doubles that share a single `_MockVendorBase`. They are the default in `provider_mode="mock"` (dev + every functional test).

A thin **factory** (`gateway.providers.build_vendors`) chooses between the two modes and tolerates missing real-vendor API keys so an operator can run the gateway with only the keys they actually have.

---

## Public surface

Importable from `gateway.providers` and its submodules:

| Symbol | Origin | Notes |
|---|---|---|
| `Vendor` | `gateway.providers.base` | ABC. The router types its `vendors` dict as `dict[str, Vendor]`. |
| `build_vendors(cfg, secrets)` | `gateway.providers` | Factory used once at boot from `app.lifespan`. |
| `REAL_VENDOR_KEY_NAMES` | `gateway.providers` | `dict[str, str]` — used by tests to assert the key-name mapping and (transitively) by `routing/refresh.py` to learn which vendors are *expected* to be configurable. |
| `OpenAIVendor` | `gateway.providers.openai` | Lazy-imported by the factory so the `openai` SDK is not loaded in mock mode. |
| `AnthropicVendor` | `gateway.providers.anthropic` | Same. |
| `GoogleVendor` | `gateway.providers.google` | Same. |
| `MockOpenAIVendor` | `gateway.providers.mock` | |
| `MockAnthropicVendor` | `gateway.providers.mock` | |
| `MockGoogleVendor` | `gateway.providers.mock` | |
| `_ScriptedResponse` | `gateway.providers.mock.<vendor>_mock` (re-exported) | Underscore-prefixed but part of the test-facing API; constructed directly when callers want to queue a multi-step script. |

The `Vendor` ABC, the three concrete real adapters, and the three concrete mock adapters are the only types the router ever touches. The factory is the only thing `app.lifespan` calls.

---

## The `Vendor` ABC

`gateway/providers/base.py:15-36`:

```python
class Vendor(ABC):
    name: str = "abstract"

    def __init__(self, secrets: SecretsManager) -> None:
        self._secrets = secrets

    @abstractmethod
    async def chat(
        self,
        model: str,
        messages: list[Message],
        params: ChatParams,
        timeout_s: float,
    ) -> ChatResult:
        """Return a normalized `ChatResult` or raise a `ProviderError`."""
```

### Contract

| Argument | Type | Source |
|---|---|---|
| `model` | `str` | The concrete vendor model id from the tier candidate (e.g. `gpt-4o-mini`), **not** the logical tier name (`fast`/`smart`). |
| `messages` | `list[Message]` | The caller's messages exactly as validated by Pydantic in `models.py:129-131`. Adapters must not mutate this list. |
| `params` | `ChatParams` | A reduced view (`max_tokens`, optional `temperature`, optional `top_p`) — see `models.py:213-218`. |
| `timeout_s` | `float` | The per-attempt budget computed by the router from `total_budget_s` − `deadline_buffer_s`, clamped to `per_attempt_max_s` (see [`router.md`](router.md)). |

The adapter **must** return a fully-populated `ChatResult` (`models.py:221-228`) on success:

```python
class ChatResult(BaseModel):
    text: str
    finish_reason: str | None = None
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    vendor_request_id: str | None = None
```

The adapter **must** raise one of:

| Exception | `ProviderErrorKind` | Retryable in router | Caller-visible (`caller_error_for`) |
|---|---|---|---|
| `RateLimited` | `rate_limited` | yes | n/a (router retries) |
| `Transient5xx` | `transient_5xx` | yes | n/a |
| `Timeout` | `timeout` | yes | n/a |
| `BadRequest` | `bad_request` | no | HTTP 400 `invalid_request` |
| `AuthError` | `auth` | no | HTTP 401 `auth` |
| `ContentFiltered` | `content_filtered` | no | HTTP 400 `content_filtered` |

See `gateway/errors.py:65-75` for the retryable/non-retryable partitioning consumed by the router loop.

### What happens if an adapter raises anything else

**ANY exception that is not a `ProviderError`** propagates past the router and FastAPI's default exception handler turns it into an unstructured **HTTP 500**. The breaker is not informed, the `Observer` is not updated, no accounting row is written, and the caller sees an opaque error. This is by design: a leaked SDK exception is a bug in the adapter, not a vendor incident. Every real adapter therefore wraps its `await self._client.<call>` in a broad chain of `except` branches that translate every documented SDK error type into one of the six `ProviderError` subclasses. The Google adapter additionally has a `except Exception` catch-all that maps to `Transient5xx` because `google-genai` 0.3.0 does not have a complete typed exception hierarchy.

### What `self._secrets` is for

`Vendor.__init__` stores a `SecretsManager` (see [`secrets.md`](secrets.md)). Real adapters call `self._secrets.get("<VENDOR>_API_KEY")` in their own `__init__` to pull the API key for the SDK client. The mock vendors ignore it (they pass `secrets=None` through to the base). Adapters never read `os.environ` directly — this is the partitioning boundary that makes both the unit tests and the in-process `MockSecretsManager` for dev work.

---

## The factory: `build_vendors(cfg, secrets)`

`gateway/providers/__init__.py:38-73`. Called exactly once from `app.lifespan` after the config is loaded and the secrets manager is constructed. Returns a `dict[str, Vendor]` keyed by `name`.

### Mock mode

```python
if cfg.provider_mode == "mock":
    return {
        "openai":    MockOpenAIVendor(secrets),
        "anthropic": MockAnthropicVendor(secrets),
        "google":    MockGoogleVendor(secrets),
    }
```

Always returns all three mock vendors regardless of which `*_API_KEY` entries the secrets manager has. The mocks don't need the keys; the goal in mock mode is "exercise every code path including ones gated by a candidate being present".

### Real mode

```python
from gateway.providers.anthropic import AnthropicVendor
from gateway.providers.google import GoogleVendor
from gateway.providers.openai import OpenAIVendor

builders: dict[str, type[Vendor]] = {
    "openai": OpenAIVendor,
    "anthropic": AnthropicVendor,
    "google": GoogleVendor,
}

out: dict[str, Vendor] = {}
for name, builder in builders.items():
    key_name = REAL_VENDOR_KEY_NAMES[name]
    if not secrets.has(key_name):
        log.warning(
            "skipping %s vendor: %s not set in secrets manager", name, key_name
        )
        continue
    try:
        out[name] = builder(secrets)
    except Exception:
        log.exception("failed to construct %s vendor; skipping", name)
if not out:
    raise RuntimeError(
        "no real vendors could be constructed; set at least one of "
        + ", ".join(REAL_VENDOR_KEY_NAMES.values())
    )
return out
```

Two design choices to highlight:

1. **Imports are deferred.** The three real-adapter modules are imported only inside `build_vendors` when `provider_mode == "real"`. This keeps the `openai`, `anthropic`, and `google-genai` SDK import cost out of dev and out of every unit test. (`pyproject.toml` pins `openai==1.58.1`, `anthropic==0.42.0`, `google-genai==0.3.0`.)

2. **Missing keys are silently skipped, not fatal.** `secrets.has(key_name)` gates construction. An operator can run the gateway against one vendor (e.g. just OpenAI) without setting the other two env vars. The set of available `name`s also feeds `routing/refresh.py`, which forces weight 0 for any candidate pointing at a provider not in the dict — so a missing key cleanly collapses the affected candidates rather than producing surprise 5xxs from the router.

### `REAL_VENDOR_KEY_NAMES`

`gateway/providers/__init__.py:31-35`:

| `name` | `SecretsManager` key |
|---|---|
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `google` | `GOOGLE_API_KEY` |

### Constructor-failure tolerance

The `except Exception: log.exception(...)` inside the loop swallows construction errors (e.g. an SDK that rejects a malformed key string at `Client(...)` time) and continues with the remaining vendors. This is consistent with the "skip if you can't, only crash if you have nothing" stance. The exception is logged at error level via `logging.exception` so it is visible in structured logs.

### Boot-time failure

If — after the loop — `out` is empty, `build_vendors` raises `RuntimeError("no real vendors could be constructed; set at least one of OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY")`. This is propagated from `app.lifespan`, which makes uvicorn fail to come up. This is intentional: a gateway with no upstreams is a paperweight.

---

## Real adapters

All three real adapters share the same shape:

1. `__init__` pulls the API key from the `SecretsManager` and constructs the vendor's async client. The client is stored on `self._client` and reused for the process lifetime.
2. `chat()` builds a vendor-specific kwargs dict, calls the SDK inside `asyncio.wait_for(..., timeout=timeout_s + 0.5)`, catches SDK exceptions and translates them, then normalizes the response.
3. None of the adapters mutate any caller-owned object.

The OpenAI and Anthropic SDKs are nearly isomorphic; the Google one is not.

### OpenAI (`gateway/providers/openai.py`)

```python
class OpenAIVendor(Vendor):
    name = "openai"

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(secrets)
        api_key = secrets.get("OPENAI_API_KEY")
        self._client = AsyncOpenAI(api_key=api_key)
```

`openai.py:38-44`.

#### Request construction

`openai.py:53-61`:

```python
kwargs: dict = {
    "model": model,
    "messages": [m.model_dump() for m in messages],
    "max_tokens": params.max_tokens,
}
if params.temperature is not None:
    kwargs["temperature"] = params.temperature
if params.top_p is not None:
    kwargs["top_p"] = params.top_p
```

`m.model_dump()` produces `{"role": ..., "content": ...}` since `Message` has only those two fields (`models.py:129-131`). `temperature` / `top_p` are omitted entirely when `None` so the SDK falls back to its own defaults rather than receiving an explicit `null`.

#### Double timeout

`openai.py:64-67`:

```python
resp = await asyncio.wait_for(
    self._client.chat.completions.create(**kwargs, timeout=timeout_s),
    timeout=timeout_s + 0.5,
)
```

Belt-and-braces: the SDK gets `timeout=timeout_s` so it can surface a clean `APITimeoutError`; `asyncio.wait_for` adds a 500 ms outer cap that fires if the SDK fails to honor its own timeout (rare but observed historically with `httpx` stream readers). Either path lands on the same `Timeout` translation.

#### Error mapping

| SDK exception | → `ProviderError` | Notes |
|---|---|---|
| `asyncio.TimeoutError` | `Timeout` | Triggered by the outer `wait_for` envelope. |
| `openai.APITimeoutError` | `Timeout` | Triggered by the SDK's own timer. |
| `openai.RateLimitError` | `RateLimited` | |
| `openai.AuthenticationError` | `AuthError` | |
| `openai.BadRequestError` | `BadRequest` | |
| `openai.InternalServerError` | `Transient5xx` | |
| `openai.APIConnectionError` | `Transient5xx` | DNS / TCP / TLS / read errors. |
| `openai.APIStatusError`, 500 ≤ status < 600 | `Transient5xx` | Catch-all for 5xxs that don't have a typed subclass. |
| `openai.APIStatusError`, status == 429 | `RateLimited` | |
| `openai.APIStatusError`, other status | `BadRequest` | Conservative catch-all. |

`openai.py:68-87`. Every branch passes `type(e).__name__` as the public `message` (so `str(exc)` is e.g. `"RateLimitError"`) and `str(e)` as `vendor_detail` — see the security note below.

#### Content-filter handling

`openai.py:89-92`:

```python
choice = resp.choices[0]
finish = choice.finish_reason
if finish == "content_filter":
    raise ContentFiltered("openai content_filter")
```

The OpenAI moderation path returns HTTP 200 with `finish_reason == "content_filter"`. The adapter therefore checks the *response* rather than relying on an exception type and raises `ContentFiltered` (non-retryable → HTTP 400 to the caller).

#### Response normalization

`openai.py:93-101`:

```python
text = choice.message.content or ""
usage = resp.usage
return ChatResult(
    text=text,
    finish_reason=finish,
    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
    vendor_request_id=getattr(resp, "id", None),
)
```

`getattr(..., 0) or 0` defends against both "attribute missing" and "attribute is `None`". `resp.id` is the vendor's request id (used downstream in the accounting row and structured logs).

### Anthropic (`gateway/providers/anthropic.py`)

```python
class AnthropicVendor(Vendor):
    name = "anthropic"

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(secrets)
        api_key = secrets.get("ANTHROPIC_API_KEY")
        self._client = AsyncAnthropic(api_key=api_key)
```

`anthropic.py:48-54`.

#### Message conversion — `_split_system`

The Anthropic Messages API puts `system` as a **top-level** field, not as a message with `role="system"`. The adapter therefore splits the conversation:

```python
def _split_system(messages: list[Message]) -> tuple[str | None, list[dict]]:
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        rest.append({"role": m.role, "content": m.content})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, rest
```

`anthropic.py:35-45`. Multiple system messages are joined with `\n\n`. If the caller provided no system messages, `system` is `None` and the SDK kwarg is omitted entirely. The remaining messages keep their original ordering and role labels.

#### Request construction

`anthropic.py:63-74`:

```python
system, conv = _split_system(messages)
kwargs: dict = {
    "model": model,
    "max_tokens": params.max_tokens,
    "messages": conv,
}
if system is not None:
    kwargs["system"] = system
if params.temperature is not None:
    kwargs["temperature"] = params.temperature
if params.top_p is not None:
    kwargs["top_p"] = params.top_p
```

Same double-timeout pattern as OpenAI (`anthropic.py:77-80`).

#### Error mapping

The Anthropic SDK exposes the same exception class names as the OpenAI SDK (these are convergent designs in the python ecosystem), so the mapping is identical:

| SDK exception | → `ProviderError` | Notes |
|---|---|---|
| `asyncio.TimeoutError` | `Timeout` | Outer `wait_for`. |
| `anthropic.APITimeoutError` | `Timeout` | |
| `anthropic.RateLimitError` | `RateLimited` | |
| `anthropic.AuthenticationError` | `AuthError` | |
| `anthropic.BadRequestError` | `BadRequest` | |
| `anthropic.InternalServerError` | `Transient5xx` | |
| `anthropic.APIConnectionError` | `Transient5xx` | |
| `anthropic.APIStatusError`, 500 ≤ status < 600 | `Transient5xx` | |
| `anthropic.APIStatusError`, status == 429 | `RateLimited` | |
| `anthropic.APIStatusError`, other status | `BadRequest` | |

`anthropic.py:81-100`.

#### Content extraction

Anthropic responses contain a **list of content blocks**, not a single string. The adapter walks the list and concatenates the `text` of every block whose `type == "text"`:

```python
text_parts: list[str] = []
for block in resp.content:
    block_type = getattr(block, "type", None)
    if block_type == "text":
        text_parts.append(getattr(block, "text", "") or "")
text = "".join(text_parts)
```

`anthropic.py:103-108`. **Tool-use blocks, image blocks, and any future block types are silently ignored.** That is acceptable today because the v1 API does not expose tool use; see Open Questions.

#### Content-filter handling

`anthropic.py:110-112`:

```python
stop_reason = resp.stop_reason
if stop_reason == "refusal":
    raise ContentFiltered("anthropic refusal")
```

Anthropic's safety signal is `stop_reason == "refusal"` on a 200 response, parallel to OpenAI's `finish_reason == "content_filter"`.

#### Response normalization

`anthropic.py:114-121`:

```python
usage = resp.usage
return ChatResult(
    text=text,
    finish_reason=stop_reason,
    input_tokens=getattr(usage, "input_tokens", 0) or 0,
    output_tokens=getattr(usage, "output_tokens", 0) or 0,
    vendor_request_id=getattr(resp, "id", None),
)
```

Note Anthropic calls them `input_tokens` / `output_tokens` (matching our `ChatResult`); OpenAI calls them `prompt_tokens` / `completion_tokens`. The accounting layer (see [`accounting.md`](accounting.md)) is downstream of this normalization and never sees the divergence.

### Google (`gateway/providers/google.py`)

```python
class GoogleVendor(Vendor):
    name = "google"

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(secrets)
        api_key = secrets.get("GOOGLE_API_KEY")
        self._client = genai.Client(api_key=api_key)
```

`google.py:44-50`. Pin: `google-genai==0.3.0` — this SDK is the youngest of the three and has churned its public surface between point releases. Several of the adapter's defensive patterns exist specifically because of that volatility (see Open Questions).

#### Message conversion — `_convert_messages`

The genai SDK uses `role="model"` (not `"assistant"`) and represents content as `{"parts": [{"text": ...}]}`. The adapter rewrites:

```python
def _convert_messages(messages: list[Message]) -> tuple[str | None, list[dict]]:
    system_parts: list[str] = []
    contents: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        role = "user" if m.role == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m.content}]})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, contents
```

`google.py:31-41`. Any non-system, non-user role (currently `assistant`, `tool`) maps to `"model"`. System messages are joined into a single `system_instruction` string the same way Anthropic system messages are joined into `system=`.

#### Request construction

The genai API takes `model`, `contents`, and a `GenerateContentConfig` rather than splaying everything into kwargs:

```python
cfg_kwargs: dict = {"max_output_tokens": params.max_tokens}
if params.temperature is not None:
    cfg_kwargs["temperature"] = params.temperature
if params.top_p is not None:
    cfg_kwargs["top_p"] = params.top_p
if system is not None:
    cfg_kwargs["system_instruction"] = system
config = genai_types.GenerateContentConfig(**cfg_kwargs)

resp = await asyncio.wait_for(
    self._client.aio.models.generate_content(
        model=model, contents=contents, config=config
    ),
    timeout=timeout_s,
)
```

`google.py:61-76`. Note `max_tokens` → `max_output_tokens`. Only the outer `asyncio.wait_for` is used; the genai 0.3.0 client does not accept a `timeout=` kwarg on its async methods.

#### Error mapping

The genai SDK does not currently expose a typed hierarchy of per-status exception classes. It raises one `genai.errors.APIError` with a `.code` attribute carrying the HTTP status. The adapter inspects that:

| Source | Status | → `ProviderError` |
|---|---|---|
| `asyncio.TimeoutError` | — | `Timeout` |
| `genai.errors.APIError` | 429 | `RateLimited` |
| `genai.errors.APIError` | 401, 403 | `AuthError` |
| `genai.errors.APIError` | ≥ 500 | `Transient5xx` |
| `genai.errors.APIError` | other / 0 | `BadRequest` |
| `Exception` (any other) | — | `Transient5xx` (defensive) |

`google.py:77-94`:

```python
except genai.errors.APIError as e:
    status = getattr(e, "code", None) or getattr(e, "status_code", None) or 0
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    if status == 429:
        raise RateLimited(...)
    if status in (401, 403):
        raise AuthError(...)
    if status >= 500:
        raise Transient5xx(...)
    raise BadRequest(...)
except Exception as e:  # pragma: no cover - defensive
    raise Transient5xx(type(e).__name__, vendor_detail=str(e)) from e
```

Two defensive details:

- `status = getattr(e, "code", None) or getattr(e, "status_code", None) or 0` — the attribute name has moved between genai versions; supporting both keeps the adapter version-tolerant. A `0` status falls into the `BadRequest` catch-all, which is the right default for "I have no idea what this is — definitely don't retry".
- The bare `except Exception → Transient5xx` is the catch-all that prevents an SDK-internal `TypeError` or `AttributeError` from leaking past the adapter as a 500. It is marked `# pragma: no cover` because there is no stable way to provoke it from tests, but its presence is load-bearing for production resilience.

#### Content extraction

Two paths, in order:

```python
text = getattr(resp, "text", None)
if text is None:
    parts: list[str] = []
    for cand in getattr(resp, "candidates", []) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", []) or []:
            t = getattr(part, "text", None)
            if t:
                parts.append(t)
    text = "".join(parts)
```

`google.py:97-106`. `resp.text` is a convenience accessor that returns the first candidate's joined text but is `None` when the response was blocked by safety. The fallback walks `candidates[].content.parts[]` manually so we still get something out of a partial response.

#### Content-filter handling

`google.py:108-115`:

```python
finish_reason = None
candidates = getattr(resp, "candidates", []) or []
if candidates:
    finish_reason = getattr(candidates[0], "finish_reason", None)
    if finish_reason is not None:
        finish_reason = str(finish_reason)
    if finish_reason and "SAFETY" in finish_reason.upper():
        raise ContentFiltered(f"google safety: {finish_reason}")
```

`finish_reason` is a `genai_types.FinishReason` enum value; we stringify it and look for the substring `SAFETY` because the enum string varies (`SAFETY`, `BLOCKLIST`, `PROHIBITED_CONTENT`, etc.). This is intentionally loose so a future enum variant doesn't slip past.

#### Response normalization

`google.py:117-127`:

```python
usage = getattr(resp, "usage_metadata", None)
input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)

return ChatResult(
    text=text or "",
    finish_reason=finish_reason,
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    vendor_request_id=getattr(resp, "response_id", None),
)
```

`usage_metadata.candidates_token_count` is genai's term for "output tokens" (it is the sum across returned candidates; we always ask for one).

### Security: `vendor_detail` is operator-only — cr-1 §4.2 resolution

Every `raise <ProviderError>(type(e).__name__, vendor_detail=str(e)) from e` in the three real adapters follows the same convention (commit `0323080`):

- The **public** message — `str(exc)` and what `ProviderError.__init__` stores via `super().__init__(message)` (`gateway/errors.py:36-38`) — is the short, safe SDK class name (e.g. `"RateLimitError"`). If the error is non-retryable it is replaced wholesale at the boundary by a fixed canonical string from `_CALLER_MESSAGES` (`errors.py:78-82`), so even the SDK class name does not reach the caller in the published 4xx body.
- The **operator-only** `vendor_detail` holds the raw SDK string, which may contain upstream response bodies, request ids, header fragments, or model-name hints. This is stored on the exception (`errors.py:38`) for structured-log use and never reaches the caller.

The pattern, from `openai.py:68-87` (anthropic.py and google.py are identical):

```python
except RateLimitError as e:
    raise RateLimited(type(e).__name__, vendor_detail=str(e)) from e
```

`caller_error_for(exc)` (`errors.py:85-110`) emits a fixed `ErrorBody.message` (`"request rejected by upstream provider"`, `"authentication failed"`, `"content filtered by upstream provider"`) — vendor_detail is intentionally excluded. This is the resolution of cr-1 §4.2; the original implementation passed `str(e)` straight into the caller-visible body.

### Related

- [`pricing.md`](pricing.md) — USD cost computation for the `ChatResult` token counts produced here; the router calls it after a successful `Vendor.chat()`.

---

## Mock adapters (`gateway/providers/mock/`)

The mock vendors exist to **exercise every router / breaker / ratelimit code path without touching the network**. They are the default in `provider_mode="mock"` (see `config.dev.yaml`), used by every functional test in `tests/test_app_e2e.py`, and the only adapters the breaker/ratelimit tests ever instantiate.

The three concrete classes are trivial — they only set `name` and `_vrid_prefix`. All behavior lives in `_MockVendorBase`.

### `_MockVendorBase` (`mock/_base_mock.py`)

#### `_ScriptedResponse`

`mock/_base_mock.py:25-37`:

```python
@dataclass(slots=True)
class _ScriptedResponse:
    result: ChatResult | None = None
    error: ProviderError | None = None
    latency_s: float = 0.0
```

Exactly one of `result` and `error` is set. `latency_s` is applied first via `asyncio.sleep`, then the result is returned or the error raised.

#### Scripting API

The base class exposes a deliberately small surface, called from tests via the same `Vendor` handle the router uses:

| Method / property | Purpose |
|---|---|
| `queue(*steps: _ScriptedResponse)` | Append raw `_ScriptedResponse` instances. |
| `queue_success(*, text=None, input_tokens=10, output_tokens=20, latency_s=0.0, vendor_request_id=None, finish_reason="stop")` | Convenience: build a `ChatResult` step. |
| `queue_error(exc: ProviderError, *, latency_s=0.0)` | Convenience: queue an exception step (with optional pre-raise latency). |
| `queue_each(steps: Iterable[_ScriptedResponse])` | Like `queue` but takes an iterable. |
| `clear()` | Drop all queued steps and reset `_call_count` to 0. Test fixtures call this between cases. |
| `call_count` | `int` property — number of times `.chat()` has been invoked since the last `clear()`. Tests use it to assert "the router actually retried twice". |

`mock/_base_mock.py:52-90`. Scripts are consumed **FIFO**: every call to `.chat()` pops `self._script.popleft()` if non-empty.

#### Default behavior (empty script)

`mock/_base_mock.py:125-137`:

```python
def _next_step(self, model: str, messages: list[Message]) -> _ScriptedResponse:
    if self._script:
        return self._script.popleft()
    # Default: deterministic success
    return _ScriptedResponse(
        result=ChatResult(
            text=self._default_text,
            finish_reason="stop",
            input_tokens=sum(len(m.content) for m in messages) // 4 + 1,
            output_tokens=len(self._default_text) // 4 + 1,
            vendor_request_id=None,
        )
    )
```

When the script is empty, the mock returns a default success — `text="ok"`, `finish_reason="stop"`, token counts derived from the message lengths (rough 4-chars-per-token heuristic with a `+1` floor so empty messages still count as 1 input token). This means a freshly-constructed mock vendor "just works" for tests that only care that the call succeeded.

#### Simulated latency

`mock/_base_mock.py:104-109`:

```python
if step.latency_s > 0:
    if step.latency_s >= timeout_s:
        # Sleep for the timeout, then raise — preserves "I waited" behaviour.
        await asyncio.sleep(min(timeout_s, step.latency_s))
        raise Timeout(f"{self.name}: simulated timeout after {timeout_s:.2f}s")
    await asyncio.sleep(step.latency_s)
```

Two cases:

- `latency_s < timeout_s` → `asyncio.sleep(latency_s)`, then proceed to return/raise the scripted outcome.
- `latency_s >= timeout_s` → `asyncio.sleep(min(timeout_s, latency_s))` (i.e. sleep for the timeout), then unconditionally raise `Timeout`. This lets tests exercise the router's deadline path without relying on a real network round-trip.

#### Deterministic `vendor_request_id`

`mock/_base_mock.py:116-122`:

```python
if result.vendor_request_id is None:
    digest = hashlib.sha256(
        f"{model}|{''.join(m.content for m in messages)}".encode()
    ).hexdigest()[:12]
    result = result.model_copy(
        update={"vendor_request_id": f"{self._vrid_prefix}-{digest}"}
    )
```

When the queued result didn't carry an explicit `vendor_request_id`, the base class synthesizes one as `f"{prefix}-{sha256(model|joined-contents)[:12]}"`. The hash is stable across runs for the same `(model, messages)`, which is invaluable when an audit-log test asserts a row's `vendor_req_id`.

### Concrete mocks

Each subclass overrides only two class attributes:

| Class | `name` | `_vrid_prefix` | File |
|---|---|---|---|
| `MockOpenAIVendor` | `openai` | `vrid-openai-mock` | `mock/openai_mock.py` |
| `MockAnthropicVendor` | `anthropic` | `vrid-anthropic-mock` | `mock/anthropic_mock.py` |
| `MockGoogleVendor` | `google` | `vrid-google-mock` | `mock/google_mock.py` |

`name` matches the `provider` value used by `TierEntry` and `CandidateRef` (see `models.py:55-60`) so the same router code finds the right vendor in mock mode. The `_vrid_prefix` is purely a debugging affordance — when a log line shows `vrid-anthropic-mock-...` you know which mock answered the call.

All three submodules also re-export `_ScriptedResponse` so test code can write:

```python
from gateway.providers.mock.openai_mock import _ScriptedResponse
```

without reaching into the underscored base module.

---

## Concurrency model

- **One instance per process.** `build_vendors` is called once from `app.lifespan`. Each `Vendor` is stored in the router's `vendors` dict and shared across every concurrent request handler on the replica. The router never creates a per-request adapter.

- **Real adapters are async-safe.** `AsyncOpenAI`, `AsyncAnthropic`, and `genai.Client(...).aio` all wrap `httpx.AsyncClient` underneath, which is safe to share across coroutines. No adapter holds per-request mutable state on `self`.

- **Mock adapters are *not* safe under true concurrency.** `_MockVendorBase` mutates `self._script` (a `collections.deque`) and `self._call_count` (a plain int) without a lock. The pop-from-the-left + integer-increment pattern would race if two `chat()` calls truly overlapped on the same instance. In practice this never happens because each test constructs its own vendor (or uses the freshly-built dict from a per-test `lifespan`) and pytest runs one event-loop step at a time. If a future test wants to fan out into the same mock from multiple tasks at once, it will need to either give each task its own mock or accept the racy semantics.

- **Per-attempt timeout is enforced by the router.** Adapters do not start their own deadlines beyond the SDK + outer `wait_for` envelope. The `timeout_s` argument arrives already shortened to fit `total_budget_s - elapsed`.

---

## Failure modes

| Failure | What happens |
|---|---|
| `secrets.get("<VENDOR>_API_KEY")` returns an empty string. | The SDK rejects it inside `Client(...)`; the resulting `Exception` is caught by `build_vendors`' inner `except`, logged via `log.exception`, and the vendor is skipped. The other two vendors still come up. |
| `secrets.has(...)` returns False for a key. | `build_vendors` logs a `warning` and skips the vendor before even importing it. |
| All three real vendors fail / are missing. | `build_vendors` raises `RuntimeError`. `app.lifespan` does not catch this; uvicorn fails to start. |
| Adapter raises a `ProviderError` subclass. | Caught by the router. Retryable kinds (`RateLimited`, `Transient5xx`, `Timeout`) feed the breaker + observer and the next candidate is tried. Non-retryable kinds (`BadRequest`, `AuthError`, `ContentFiltered`) are turned into the correct HTTP status by `caller_error_for` (`errors.py:85-110`). |
| Adapter raises something **other** than a `ProviderError`. | Propagates past the router → FastAPI default exception handler → HTTP 500 with no structured body. The breaker is **not** updated, the observer is **not** updated, no audit row is written for that attempt. This is a bug; the test suite covers every documented SDK exception type to keep it from happening. |
| SDK call hangs past `timeout_s + 0.5`. | The outer `asyncio.wait_for` cancels the task → `asyncio.TimeoutError` → `Timeout`. The mock vendors simulate this path directly. |
| Anthropic returns a response with no `text` blocks (e.g. only tool-use). | The adapter returns `ChatResult(text="", ...)`. The router treats this as a successful call. |
| Google returns a 200 with `finish_reason` containing `SAFETY`. | `ContentFiltered` → HTTP 400 to caller, non-retryable. |
| Google SDK raises an `Exception` that is not `APIError`. | The defensive catch-all maps it to `Transient5xx` so the router retries against another candidate. |

The router's contract (see [`router.md`](router.md)) is that *every* exception out of `Vendor.chat()` is a `ProviderError`. The adapters are responsible for upholding that contract; the catch-alls and `getattr(..., default)` defenses above are how they do it.

---

## Configuration knobs

| Knob | Location | Effect |
|---|---|---|
| `provider_mode` | `config.yaml` top-level, `models.py:48,99` | `"mock"` → all three mock vendors; `"real"` → real adapters gated by present keys. Overridable via the `GATEWAY_PROVIDER_MODE` env var (see [`config.md`](config.md)). |
| `secrets_mode` | `config.yaml` top-level, `models.py:49,100` | `"env"` → API keys come from `os.environ`; `"mock"` → in-memory `MockSecretsManager`. See [`secrets.md`](secrets.md). |
| `OPENAI_API_KEY` | env (when `secrets_mode=env`) | Required for `OpenAIVendor`. |
| `ANTHROPIC_API_KEY` | env (when `secrets_mode=env`) | Required for `AnthropicVendor`. |
| `GOOGLE_API_KEY` | env (when `secrets_mode=env`) | Required for `GoogleVendor`. |
| `timeout_s` | computed per-call by the router from `total_budget_s`, `per_attempt_max_s`, `deadline_buffer_s` | Not configurable on the vendor itself; the adapter is a pure function of its argument. |

There are intentionally **no per-vendor config blocks** (base URL, organization id, project id, etc.). If any of those become necessary, the right place to add them is on the adapter `__init__`, plumbed from `Config` via `build_vendors`.

---

## Open questions / known gaps

- **Anthropic content extraction ignores non-text blocks.** The adapter at `anthropic.py:103-108` only concatenates `type == "text"` blocks. Tool-use, image, and any future block types are silently dropped. This is correct for the v1 API (which does not surface tool use) but will need to be revisited if/when tool use is added to the gateway. See cr-1 §4 / t-1 §17 missing-scenarios.

- **`google-genai` 0.3.0 is volatile.** The SDK has shifted its `APIError` attribute names (`.code` ↔ `.status_code`) between point releases. The adapter handles this defensively but a major-version bump may require revisiting the error-mapping branch. Tracking dependency: when the SDK promotes a typed-exception hierarchy, the catch-all `except Exception → Transient5xx` should be tightened. See t-1 §18.

- **OpenAI `APIStatusError` "other" branch is broad.** Any non-5xx, non-429 status falls into `BadRequest`. In particular a 408 (Request Timeout, rare but possible) would land here rather than `Timeout`, and a 503 served as `APIStatusError` rather than `InternalServerError` is handled by the 5xx branch only because of the explicit `500 <= e.status_code < 600` check above it. The current mapping is conservative and safe (no incorrect retries) but a future refinement could surface more nuance.

- **No streaming.** `ChatCompletionRequest._no_streaming_in_v1` (`models.py:157-162`) rejects `stream=true` before any vendor is selected. Adding streaming would require new methods on `Vendor` (probably `async def stream(...) -> AsyncIterator[ChatChunk]`) and a parallel set of error-mapping branches; the current adapters are non-streaming-only by design. See `architecture.md` §1.

- **Mock vendors are not concurrency-safe.** As noted under [Concurrency model](#concurrency-model), `_MockVendorBase` mutates `_script` and `_call_count` without synchronization. This is fine for the current test suite but worth flagging if anyone ever wants to run mock-mode load tests.

- **Constructor-failure observability.** A vendor that fails to construct in real mode is logged via `log.exception` and skipped, but no Prometheus metric records the event. Operators have to read the boot logs to learn that a vendor was silently dropped. See cr-1 §6.

---

## Cross-references

- [`../architecture.md`](../architecture.md) — overall component map, especially §6 (failover & deadline) and §7 (resilience design).
- [`router.md`](router.md) — the only consumer of `Vendor.chat()`. Documents the failover loop, the `timeout_s` derivation, and how `ProviderError` kinds drive the retry decision.
- [`secrets.md`](secrets.md) — `SecretsManager` ABC and the env / mock implementations the adapters depend on.
- [`observability.md`](observability.md) — `ProviderErrorKind` enum, structured-log fields (`vendor_detail` flows here), and the per-attempt Prometheus counter labels.
- [`config.md`](config.md) — `provider_mode` and `secrets_mode` config plumbing.
