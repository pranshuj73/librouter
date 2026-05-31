# `gateway/auth.py` — bearer-token resolution

## Purpose

`CallerResolver` is the single boundary between raw `Authorization: Bearer …`
headers and validated `Caller` identities. It hashes the bearer token with
HMAC-SHA256 (keyed by a server-side pepper), looks up the corresponding
`callers` row in Postgres (via the `_DBProtocol` it depends on), caches the
result in-process for 60 seconds, and returns either a `Caller` or `None`.

This module is small but security-critical: it is the *only* trust boundary on
inbound requests. Everything downstream — daily-cap enforcement, the Router,
the audit log — trusts the returned `Caller.name`.

Hash format and lookup live here. The storage schema for `callers` lives in
[`db.md`](db.md). Boot wiring lives in [`app.md`](app.md). The pepper itself
is stored in the `SecretsManager` under `GATEWAY_KEY_HASH_PEPPER`
(see [`secrets.md`](secrets.md)).

## Public surface

| Symbol | Signature | Notes |
|---|---|---|
| `hash_api_key` | `(raw: str, *, pepper: str) -> str` | Module-level pure function. Returns `"v2:hmac-sha256:" + hexdigest`. Raises `ValueError` on a falsy pepper. |
| `CallerResolver` | `class` | One instance per process. |
| `CallerResolver.__init__` | `(*, db, pepper: str, cache_ttl_s=60.0, cache_maxsize=10_000, now_s_fn=time.monotonic)` | All knobs are constructor-only. `pepper` is required and must be non-empty. |
| `CallerResolver.resolve_bearer` | `async (header: str \| None) -> Caller \| None` | The only public method. |

`_DBProtocol` is module-private; any object with
`async caller_by_key_hash(key_hash: str) -> Caller | None` satisfies it
(structurally). The production wiring uses `Database` from `db.py`.

## Internals

### `hash_api_key`

`auth.py:22-26`:

```python
def hash_api_key(raw: str, *, pepper: str) -> str:
    if not pepper:
        raise ValueError("GATEWAY_KEY_HASH_PEPPER must be a non-empty string")
    digest = hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return "v2:hmac-sha256:" + digest
```

The `v2:hmac-sha256:` prefix is part of the stored value, not metadata. Both
the `callers.key_hash` column (see [`db.md`](db.md)) and the seeding output
of `scripts/seed_callers.py` include it. The `v2:` family marker exists to
distinguish the current HMAC scheme from the pre-migration bare
`sha256:<hex>` format; **bare `sha256:` hashes are not accepted by the
resolver**. There is no dual-read fallback — operators re-seeding from
plaintext via `scripts/seed_callers.py` is the migration path.

The pepper is loaded from the `SecretsManager` once at boot
(`app.py:171`: `pepper = secrets.get("GATEWAY_KEY_HASH_PEPPER")`) and
passed into `CallerResolver(pepper=pepper)` (`app.py:208`). A missing
or empty pepper raises `KeyError` from `SecretsManager.get` which crashes
the lifespan — fail-loud.

### `CallerResolver._cache`

`auth.py:64`:

```python
self._cache: OrderedDict[str, tuple[float, Caller | None]] = OrderedDict()
```

Cache key: the *hashed* token (`"v2:hmac-sha256:..."`). Raw tokens are never
stored in-process beyond the lifetime of `resolve_bearer`'s local variable.

Cache value: a 2-tuple `(inserted_at, caller_or_none)`. The `Caller | None`
half means **both positive and negative results are cached** with the same TTL
(`auth.py:74-79`, `auth.py:84`). The rationale: under steady traffic, a
client mis-typing a token would otherwise hammer the DB. Negative caching makes
unknown-token rates uncorrelated with DB load.

### Cache lookup logic

`auth.py:75-79`:

```python
cached = self._cache.get(key_hash)
if cached is not None and now - cached[0] < self._ttl:
    self._cache.move_to_end(key_hash)
    return cached[1]
```

The boundary is **strict `<`**: an entry with `inserted_at = T` is valid for
`now ∈ [T, T + ttl)`. At exactly `T + ttl` the entry is considered stale and a
fresh DB lookup is performed. This matches what `tests/test_auth.py`
(`test_cache_expires_after_ttl`) asserts.

### LRU eviction

The cache is bounded to `cache_maxsize` (default `10_000`) entries via an
`OrderedDict` with LRU-by-access semantics:

- Cache hit: `move_to_end(key_hash)` marks the entry as most-recently-used
  (`auth.py:78`).
- Cache miss (after DB lookup): the new `(now, caller_or_none)` tuple is
  inserted and immediately `move_to_end`'d (covers the "updated stale entry"
  case where the key already existed) (`auth.py:84-86`).
- Over-capacity check: `if len(self._cache) > self._maxsize:
  self._cache.popitem(last=False)` drops the oldest-by-access
  (`auth.py:88-89`).

The cache cannot grow beyond `cache_maxsize` entries regardless of how many
unique bearer tokens an adversary spams.

### `resolve_bearer`

End-to-end flow (`auth.py:66-90`):

1. Reject `None` / missing prefix / empty token after stripping. Return `None`
   without touching the cache or DB.
2. Hash the token with `hash_api_key(token, pepper=self._pepper)`.
3. Check the cache. If fresh (`now - inserted_at < ttl`), bump LRU, return.
4. Call `db.caller_by_key_hash(key_hash)`. If the returned `Caller` has
   `enabled=False`, coerce it to `None` *before* caching. The cache entry then
   looks identical to "no such caller".
5. Insert `(now, caller_or_none)` into the cache, `move_to_end`, and evict the
   oldest if over capacity.
6. Return the caller (or `None`).

### `now_s_fn`

Injected as `time.monotonic` by default. Tests pass a controllable callable
(see `tests/test_auth.py::test_cache_expires_after_ttl`) to advance "time"
deterministically. `time.monotonic` is correct here because:

- The cache is process-local; no cross-process time comparison.
- Wall-clock drift does not affect TTL semantics.
- Monotonic time cannot go backwards across NTP adjustments.

## Concurrency model

- **Single in-process instance** of `CallerResolver`, constructed once at
  `app.py:208` and reused for every request.
- **`_cache` is an `OrderedDict`, mutated without a lock.** This is safe because
  every mutation happens on the asyncio event loop thread: `resolve_bearer` is
  the only writer, and it never yields between cache read and cache write
  except across a single `await self._db.caller_by_key_hash(...)`. During that
  yield, another concurrent `resolve_bearer` for the same `key_hash` could fire
  a second DB lookup; both will then write the same value. This is a benign
  "thundering herd of two" — neither correctness nor monotonicity is
  compromised.
- **No per-key locking.** A short burst of unique-token requests will issue one
  DB lookup per token. The cache absorbs steady-state traffic; bursts cost
  Postgres connections.

## Failure modes

| Scenario | Behavior |
|---|---|
| Header is `None` or missing `Bearer ` prefix | Return `None`. No cache touch, no DB call. The endpoint returns 401. |
| Header is `"Bearer "` with empty / whitespace-only token | Return `None`. Same as above. |
| Unknown token | `db.caller_by_key_hash` returns `None` → cached as `None` for 60s → return `None`. |
| Known token, `enabled=False` | Coerced to `None` *before* caching (`auth.py:82-83`). Indistinguishable from "unknown". |
| Known token, just disabled in DB | Cached as a valid `Caller` until TTL expires. **Up to 60s of stale acceptance.** This is the known revocation window. |
| `db.caller_by_key_hash` raises | Exception propagates out of `resolve_bearer`. No cache entry written. The endpoint converts to a 500. Subsequent requests for the same token will re-hit the DB. |
| Cache full, new lookup | `popitem(last=False)` evicts the oldest-by-access entry before returning. |
| Falsy `pepper` at construction or hashing | `ValueError("GATEWAY_KEY_HASH_PEPPER must be a non-empty string")`. The boot path raises *before* the resolver is built because `secrets.get(...)` raises first. |

### Known sharp edges (from [`cr-1.md`](../../code-review/cr-1.md))

- **§3.1 — bare SHA-256, no salt/pepper.** Resolved in commit `4bcccd4`:
  caller keys are now hashed with HMAC-SHA256 keyed by a server-side pepper
  (`GATEWAY_KEY_HASH_PEPPER`) loaded from the `SecretsManager`. Hash format
  is `v2:hmac-sha256:<hex>`; pre-migration `sha256:<hex>` values must be
  re-seeded via `scripts/seed_callers.py`.
- **§3.4 — unbounded auth cache.** Resolved in commit `9eaa260`: the cache
  is an `OrderedDict` with `cache_maxsize=10_000` default and LRU
  eviction via `popitem(last=False)`.
- **§3.6 — 60s revocation window.** Still open. Disabling a caller takes
  effect within `cache_ttl_s`. There is no eager invalidation. For an
  emergency revoke the operator's only lever today is rolling the process.
  A pub/sub invalidation channel is not implemented.

## Configuration knobs

| Knob | Constructor arg | Default | Effect |
|---|---|---|---|
| HMAC pepper | `pepper` | (required) | Server-side secret HMAC key. Loaded from `SecretsManager["GATEWAY_KEY_HASH_PEPPER"]` at boot. Falsy values raise. |
| Cache TTL | `cache_ttl_s` | `60.0` | Trade-off between DB load and revocation window. |
| Cache size cap | `cache_maxsize` | `10_000` | LRU eviction at this many entries. |
| Time source | `now_s_fn` | `time.monotonic` | Injected for tests. |
| DB | `db` | (required) | Anything satisfying `_DBProtocol`. Wired to `Database` at `app.py:208`. |

These are constructor-only. The wiring site uses defaults for everything
except `db` and `pepper` (`CallerResolver(db=db, pepper=pepper)`), so
changing TTL today requires editing `app.py`.

## Open questions / known gaps

- **No pub/sub invalidation.** A 60s window is acceptable for routine caller
  management but inappropriate for incident response. Adding a Redis pub/sub
  channel that pushes `INVALIDATE <key_hash>` would cap the window to network
  RTT. ([`cr-1.md`](../../code-review/cr-1.md) §3.6 remains open.)
- **Pepper rotation requires a re-seed.** Rotating
  `GATEWAY_KEY_HASH_PEPPER` invalidates every stored `key_hash`. The
  migration path is: rotate the pepper secret, re-run
  `scripts/seed_callers.py` against the same plaintext keys (held in
  `GATEWAY_SEED_KEY_<NAME>` env vars), and roll replicas. There is no
  dual-pepper transition window.
- **Cache key is the hash, not the raw token.** This is desirable from a
  defense-in-depth standpoint (a memory dump leaks hashes, not tokens) but it
  means we hash on every request. HMAC-SHA256 of a short token is cheap; if this
  became a hot spot, the hash could be cached on the FastAPI request scope.
- **`resolve_bearer` does not log on failure.** A spike in unknown-token
  attempts is invisible from this module alone. Operators would need to add a
  middleware-level counter.
- **Test coverage** ([`../code-review/t-1.md`](../../code-review/t-1.md) §3):
  covers happy path, missing header, wrong scheme, unknown key, disabled
  caller, cache hit, TTL expiry, negative caching. Missing: LRU-eviction
  behaviour at the size cap; concurrent in-flight lookups for the same token.

## Cross-references

- DB lookup: [`modules/db.md`](db.md) — `Database.caller_by_key_hash` and the
  `callers` schema.
- Pepper storage: [`modules/secrets.md`](secrets.md) —
  `GATEWAY_KEY_HASH_PEPPER` is held by the `SecretsManager` singleton on
  `app.state.secrets`.
- Caller seeding: `scripts/seed_callers.py` hashes plaintexts with the same
  pepper and upserts via `Database.upsert_caller`.
- Boot wiring: [`modules/app.md`](app.md) — pepper load at `app.py:171`,
  resolver construction at `app.py:208`.
- Architecture context: [`architecture.md`](../architecture.md) §4 (state
  partitioning row for "Caller auth lookup"), §7 (resilience row for
  "Caller's auth cache TTL").
- Hash format consumers: `scripts/seed_callers.py` and the
  `callers.key_hash` column.
- Code-review references: [`../code-review/cr-1.md`](../../code-review/cr-1.md) §3.1 (resolved), §3.4 (resolved), §3.6 (open).
- Test review: [`../code-review/t-1.md`](../../code-review/t-1.md) §3.
