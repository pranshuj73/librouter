# `gateway/pricing.py` — USD cost lookup

## Purpose

Compute the USD cost of one completed `Vendor.chat()` attempt, given the vendor-reported `input_tokens` and `output_tokens`. The single consumer is `Router._record` (`gateway/router.py:288-305`), which calls `cost_usd(...)` for every attempt that produced a token count and writes the result into the `AttemptRecord.cost_usd` field that the accounting layer eventually persists and exports to Prometheus (`gateway/metrics.py:61` — `gateway_cost_usd_total`).

The implementation is a flat O(1) lookup over a **vendored snapshot** of the LiteLLM `model_prices_and_context_window.json` file (`gateway/data/model_prices_and_context_window.json`). That JSON is the same one LiteLLM publishes; we ship a frozen copy so the gateway has no runtime dependency on the LiteLLM package, no network fetch at boot, and no surprise price drift between deploys.

This module is a deliberate replacement for the older `Config.prices` block (which carried `input` / `output` USD-per-1M-token rates per `(provider, model)` in `config.yaml`). The Router still falls back to `Config.prices` for any candidate not present in the JSON (`router.py:295-305`) so the migration can happen one tier at a time — see Open Questions.

---

## Public surface

Importable from `gateway.pricing`:

| Symbol | Kind | Defined at | Notes |
|---|---|---|---|
| `load_pricing(json_path=None)` | function | `pricing.py:130-140` | Read the JSON, build the index, return a `PricingTable`. Defaults to the vendored file path. |
| `PricingTable` | class | `pricing.py:38-85` | The lookup object the router holds. |
| `PricingTable.has(*, provider, model)` | method | `pricing.py:47-49` | Cheap membership test — used by the router to decide between this table and the `Config.prices` fallback. |
| `PricingTable.cost_usd(*, provider, model, input_tokens, output_tokens)` | method | `pricing.py:51-78` | Return USD cost; `0.0` (logged once) when the pair is unknown. |
| `PricingTable.context_window(*, provider, model)` | method | `pricing.py:80-85` | Return `max_input_tokens` for the pair, or `None`. Not consumed today; surfaced for future use. |

The `_Entry` `NamedTuple` and `_build_index` helper are private.

---

## Internals

### Provider-name mapping

`_PROVIDER_LITELLM_MAP` (`pricing.py:25-29`) bridges the gateway's internal provider names (`openai`, `anthropic`, `google` — see `gateway/providers/__init__.py:31-35`) to the `litellm_provider` field in the JSON:

```python
_PROVIDER_LITELLM_MAP: dict[str, tuple[str, ...]] = {
    "openai": ("openai",),
    "anthropic": ("anthropic",),
    "google": ("vertex_ai-language-models", "vertex_ai"),
}
```

| Internal | LiteLLM `litellm_provider` values | First-match wins |
|---|---|---|
| `openai` | `openai` | — |
| `anthropic` | `anthropic` | — |
| `google` | `vertex_ai-language-models`, `vertex_ai` | Yes — `_build_index` builds a reverse map via `setdefault`, so the first listed value wins on collisions. |

The tuple form exists because Google's pricing appears in the JSON under more than one `litellm_provider` value across model generations; listing both keeps every Gemini SKU reachable without forking the JSON.

### Index construction

`_build_index(raw)` (`pricing.py:88-127`) iterates the raw JSON dict once. For each `model_key → info`:

1. Skip `sample_spec` (the documentation row at the top of the LiteLLM file).
2. Skip non-dict entries (defensive — the LiteLLM file is hand-edited).
3. Look up `info["litellm_provider"]` in the reverse map; if it doesn't match an internal provider, **silently skip**. (This is intentional: we don't want a pricing entry for `bedrock` or `cohere` showing up under any of our internal provider names.)
4. Require both `input_cost_per_token` and `output_cost_per_token`; if either is missing, skip.
5. Read `max_input_tokens`, tolerating both numeric values and the string-typed values that appear in `sample_spec`-style documentation rows.
6. Insert `(our_provider, model_key) → _Entry(...)`.

The index is built once at `load_pricing()` time; every subsequent `cost_usd` call is a dict lookup. There is no time-bounded refresh, no expiry, and no I/O after construction.

### Cost computation

`cost_usd` (`pricing.py:51-78`) is straightforward:

```python
entry = self._index.get((provider, model))
if entry is None:
    # log once, return 0.0
    ...
return (
    input_tokens * entry.input_cost_per_token
    + output_tokens * entry.output_cost_per_token
)
```

Note the costs in the JSON are **per-token**, not per-million; no extra division is applied. The unit test `tests/test_pricing.py` exercises this directly with known fixtures.

### Missing-model fallback

On an unknown `(provider, model)` pair, `cost_usd` returns `0.0` and logs a warning **at most once per pair** — the `self._warned: set[tuple[str, str]]` deduplicates subsequent calls (`pricing.py:43, 67-73`). This keeps the audit row consistent (`cost_usd=0.0`) and avoids log spam when the same misconfigured candidate gets routed to thousands of times.

The router checks `has()` before calling `cost_usd()` and falls back to `Config.prices` (per-million-token rates) when the JSON has no entry (`router.py:288-305`). The two pricing sources are therefore deliberately disjoint: per-token rates from the JSON for indexed models, per-million-token rates from YAML for the rest. The fallback exists to ease migration; see Open Questions.

---

## Concurrency

- `PricingTable` is constructed once at `Router.__init__` time (`gateway/router.py:119`) and shared across every concurrent request handler.
- All lookup methods (`has`, `cost_usd`, `context_window`) are pure dict reads after construction. Safe under unlimited concurrent reads.
- The only mutable state is `self._warned`, a `set` that `cost_usd` mutates on unknown pairs. `set.add` is atomic under the GIL and the contents are never read for control flow, so the worst case under a true race is a duplicated warning line — never a wrong cost.
- No file I/O after `load_pricing` returns.

---

## Failure modes

| Trigger | Where | Effect |
|---|---|---|
| Vendored JSON file missing or unreadable | `load_pricing` `open(path, ...)` | Raises `FileNotFoundError` / `OSError`. Surfaces from `Router.__init__` and aborts `app.lifespan`. |
| Vendored JSON not valid JSON | `json.load(fh)` | Raises `json.JSONDecodeError`. Same outcome. |
| Entry missing `input_cost_per_token` / `output_cost_per_token` | `_build_index` | Entry silently skipped. Subsequent lookups return `0.0` via the unknown-pair path. |
| Entry has `max_input_tokens` as a string (sample_spec doc rows) | `_build_index` | Replaced with `None`. `context_window()` returns `None`. |
| Unknown `(provider, model)` at lookup time | `cost_usd` | `0.0` returned, single warning log line, router's `has()` check normally diverts to `Config.prices` fallback first. |
| `_PROVIDER_LITELLM_MAP` collision (two internal providers claim the same LiteLLM value) | `_build_index` reverse map | `setdefault` keeps the first internal provider added; later providers silently lose. None occur today. |

The module never raises during steady-state lookups — every failure mode is either a boot-time hard fail or a silent zero with a logged warning.

---

## Configuration knobs

| Knob | Location | Effect |
|---|---|---|
| Vendored JSON file | `gateway/data/model_prices_and_context_window.json` | The entire pricing surface. Replace the file to update prices. |
| `_PROVIDER_LITELLM_MAP` | `pricing.py:25-29` | Adding a new internal provider requires extending this map with the matching `litellm_provider` value(s). |
| `json_path` arg | `load_pricing(json_path=...)` | Test affordance — point at a fixture JSON to make assertions deterministic (used by `tests/test_pricing.py`). |
| `Config.prices` (fallback) | `config.yaml`, `models.py:103` | Per-million-token rates for any `(provider, model)` not in the vendored JSON. Consumed by `Router._record`'s fallback branch. |

There is no env var, no SIGHUP path, and no rate-limit on the warning logger; the table is fully static for the process lifetime.

---

## Open questions / known gaps

- **Vendored snapshot drift.** The JSON is a frozen copy of the LiteLLM file at the time the gateway was last built. Vendor prices move (OpenAI cut several SKUs by ~30% in late 2024; Google reorganised Gemini SKUs twice in 2025); operators must manually copy a fresh `model_prices_and_context_window.json` into `gateway/data/` and rebuild the image to pick up the change. There is currently no refresh script, no CI check that the file is recent, and no diff alert when prices change. A `scripts/refresh_pricing.py` that fetches the latest LiteLLM JSON and runs a smoke comparison would close this gap.
- **Fallback ambiguity.** Router uses `_pricing.has()` to choose between the per-token JSON path and the per-million-token `Config.prices` path. A typo in a tier's `model` field that happens to land outside the JSON will silently divert to `Config.prices`, where another typo could leave the price at zero. Operators have no on-startup warning that a configured tier candidate is using the fallback path. Removing `Config.prices` once every tier is migrated to the JSON would eliminate this hazard.
- **Vertex AI vs. AI Studio.** The Google adapter (`gateway/providers/google.py:50`) uses the **AI Studio** API key path (`genai.Client(api_key=...)`), but the JSON entries we match are `vertex_ai-language-models` and `vertex_ai`. Pricing is the same model-by-model in practice today, but if Google ever divergent-prices the two surfaces the lookup will silently use the Vertex number. See `providers.md` for the AI Studio decision.
- **No tracking of cache-token / batch / fine-tune rates.** The JSON carries fields like `input_cost_per_token_batches`, `cache_creation_input_token_cost`, etc. `_build_index` ignores them; `cost_usd` therefore overcharges callers using batched APIs and undercharges those benefiting from prompt-caching discounts. Neither code path is wired up today (the gateway makes synchronous, non-cached calls), so this is a latent gap rather than an active bug.
- **No `Decimal` precision.** Cost is `float`, accumulated as `float` in the metric counter and persisted as `FLOAT` in Postgres (`gateway/db.py:182`). At gateway scale (per-request costs in the 10⁻⁵–10⁻³ USD range), `float64` precision is fine, but a future billing pipeline that needs cent-accurate totals will want `Decimal` end-to-end.

---

## Cross-references

- [`providers.md`](providers.md) — produces the `ChatResult.input_tokens` / `output_tokens` this module prices.
- [`router.md`](router.md) — only caller of `PricingTable.has` / `cost_usd`; documents the `Config.prices` fallback decision.
- [`accounting.md`](accounting.md) — persists `AttemptRecord.cost_usd` produced here.
- [`observability.md`](observability.md) — `gateway_cost_usd_total` Prometheus counter.
