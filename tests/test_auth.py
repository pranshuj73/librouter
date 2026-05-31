"""Tests for gateway/auth.py.

TDD step 13. CallerResolver maps `Authorization: Bearer <key>` -> Caller via
HMAC-SHA256(pepper, key) lookup with a 60s in-process cache.
"""

from __future__ import annotations

import pytest

from gateway.auth import CallerResolver, hash_api_key
from gateway.models import Caller


pytestmark = pytest.mark.asyncio

# Fixed test pepper — used consistently across all fixtures in this module.
_TEST_PEPPER = "test-pepper-32-bytes-of-not-real-entropy"


class _StubDB:
    def __init__(self, by_hash: dict[str, Caller]) -> None:
        self._by_hash = by_hash
        self.lookups = 0

    async def caller_by_key_hash(self, key_hash: str) -> Caller | None:
        self.lookups += 1
        return self._by_hash.get(key_hash)


# ---------------------------------------------------------------- hash_api_key unit tests


async def test_hash_api_key_requires_pepper():
    """hash_api_key('foo', pepper='') must raise ValueError."""
    with pytest.raises(ValueError, match="GATEWAY_KEY_HASH_PEPPER"):
        hash_api_key("foo", pepper="")


async def test_hash_api_key_format():
    """Result starts with 'v2:hmac-sha256:' and the hex part is exactly 64 chars."""
    h = hash_api_key("hello", pepper=_TEST_PEPPER)
    prefix = "v2:hmac-sha256:"
    assert h.startswith(prefix)
    hex_part = h[len(prefix):]
    assert len(hex_part) == 64
    # Ensure it is valid hex.
    int(hex_part, 16)


async def test_hash_api_key_pepper_changes_output():
    """Same raw key with two different peppers produces different hashes."""
    h1 = hash_api_key("same-raw-key", pepper="pepper-one")
    h2 = hash_api_key("same-raw-key", pepper="pepper-two")
    assert h1 != h2


async def test_hash_api_key_deterministic():
    """Same raw key + same pepper always produces the same hash."""
    h1 = hash_api_key("stable-key", pepper=_TEST_PEPPER)
    h2 = hash_api_key("stable-key", pepper=_TEST_PEPPER)
    assert h1 == h2


async def test_resolve_valid_key():
    key = "secret-123"
    db = _StubDB(
        {hash_api_key(key, pepper=_TEST_PEPPER): Caller(name="svc-a", daily_token_cap=1000, enabled=True)}
    )
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    caller = await r.resolve_bearer(f"Bearer {key}")
    assert caller is not None
    assert caller.name == "svc-a"


async def test_missing_header_returns_none():
    db = _StubDB({})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    assert await r.resolve_bearer(None) is None


async def test_wrong_scheme_returns_none():
    db = _StubDB({})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    assert await r.resolve_bearer("Basic abc") is None


async def test_unknown_key_returns_none():
    db = _StubDB({})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    assert await r.resolve_bearer("Bearer nope") is None


async def test_disabled_caller_returns_none():
    key = "secret-xyz"
    db = _StubDB(
        {hash_api_key(key, pepper=_TEST_PEPPER): Caller(name="svc-z", daily_token_cap=100, enabled=False)}
    )
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    assert await r.resolve_bearer(f"Bearer {key}") is None


async def test_cache_hits_avoid_db_lookup():
    key = "secret-abc"
    db = _StubDB(
        {hash_api_key(key, pepper=_TEST_PEPPER): Caller(name="svc-a", daily_token_cap=1000, enabled=True)}
    )
    clock = [0.0]
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0, now_s_fn=lambda: clock[0])
    assert await r.resolve_bearer(f"Bearer {key}") is not None
    assert db.lookups == 1
    # Still inside TTL
    clock[0] = 30.0
    assert await r.resolve_bearer(f"Bearer {key}") is not None
    assert db.lookups == 1


async def test_cache_expires_after_ttl():
    key = "secret-abc"
    db = _StubDB(
        {hash_api_key(key, pepper=_TEST_PEPPER): Caller(name="svc-a", daily_token_cap=1000, enabled=True)}
    )
    clock = [0.0]
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0, now_s_fn=lambda: clock[0])
    await r.resolve_bearer(f"Bearer {key}")
    clock[0] = 61.0
    await r.resolve_bearer(f"Bearer {key}")
    assert db.lookups == 2


async def test_negative_cache_after_ttl_too():
    db = _StubDB({})
    clock = [0.0]
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0, now_s_fn=lambda: clock[0])
    await r.resolve_bearer("Bearer nope")
    assert db.lookups == 1
    clock[0] = 30.0
    await r.resolve_bearer("Bearer nope")
    # Cached miss within TTL
    assert db.lookups == 1
    clock[0] = 90.0
    await r.resolve_bearer("Bearer nope")
    assert db.lookups == 2


async def test_cache_evicts_oldest_on_overflow():
    """LRU eviction: oldest entry is dropped when cache_maxsize is exceeded."""
    keys = [f"key-{i}" for i in range(5)]
    db = _StubDB(
        {hash_api_key(k, pepper=_TEST_PEPPER): Caller(name=f"svc-{i}", daily_token_cap=100, enabled=True)
         for i, k in enumerate(keys)}
    )
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0, cache_maxsize=4)

    # Fill the cache with the first 4 keys (keys[0] is oldest).
    for k in keys[:4]:
        await r.resolve_bearer(f"Bearer {k}")
    assert len(r._cache) == 4

    # Inserting a 5th key must evict the oldest (keys[0]).
    await r.resolve_bearer(f"Bearer {keys[4]}")
    assert len(r._cache) == 4
    assert hash_api_key(keys[0], pepper=_TEST_PEPPER) not in r._cache, "keys[0] should have been evicted"

    # Resolving keys[0] again must go to the DB (evicted → cache miss).
    lookups_before = db.lookups
    await r.resolve_bearer(f"Bearer {keys[0]}")
    assert db.lookups == lookups_before + 1

    # keys[2]-keys[4] were never evicted; they should still be cached.
    # (keys[1] may have been evicted when keys[0] was re-inserted above.)
    lookups_before = db.lookups
    for k in keys[2:]:
        await r.resolve_bearer(f"Bearer {k}")
    assert db.lookups == lookups_before


async def test_cache_lru_keeps_recent_entries():
    """Re-reading an entry promotes it so it is not evicted by a later insert."""
    keys = ["lru-a", "lru-b", "lru-c", "lru-d"]
    db = _StubDB(
        {hash_api_key(k, pepper=_TEST_PEPPER): Caller(name=k, daily_token_cap=100, enabled=True)
         for k in keys}
    )
    clock = [0.0]
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0, cache_maxsize=3,
                       now_s_fn=lambda: clock[0])

    # Resolve lru-a, lru-b, lru-c — cache is now full (lru-a is oldest).
    for k in keys[:3]:
        await r.resolve_bearer(f"Bearer {k}")

    # Re-read lru-a within TTL → moves lru-a to most-recently-used;
    # lru-b becomes oldest.
    clock[0] = 10.0
    await r.resolve_bearer("Bearer lru-a")

    # Insert lru-d → lru-b (now oldest) must be evicted; lru-a, lru-c, lru-d remain.
    await r.resolve_bearer("Bearer lru-d")
    assert len(r._cache) == 3

    cached_keys = set(r._cache.keys())
    assert hash_api_key("lru-b", pepper=_TEST_PEPPER) not in cached_keys, "lru-b should have been evicted"
    for k in ["lru-a", "lru-c", "lru-d"]:
        assert hash_api_key(k, pepper=_TEST_PEPPER) in cached_keys, f"{k} should still be cached"

    # Confirm lru-b goes to DB on next access.
    lookups_before = db.lookups
    await r.resolve_bearer("Bearer lru-b")
    assert db.lookups == lookups_before + 1


async def test_negative_cache_also_bounded():
    """Pumping invalid tokens does not let the cache grow past cache_maxsize."""
    db = _StubDB({})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0, cache_maxsize=5)

    for i in range(20):
        await r.resolve_bearer(f"Bearer invalid-token-{i}")

    assert len(r._cache) == 5


# ---------------------------------------------------------------- new tests (t-1 §3)


async def test_empty_bearer_after_prefix_returns_none():
    """`Bearer ` (trailing space, nothing else) -> None.

    Covers the `if not token` branch in auth.py.
    """
    db = _StubDB({})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    assert await r.resolve_bearer("Bearer ") is None
    # And no DB lookup should have occurred.
    assert db.lookups == 0


async def test_bearer_with_only_whitespace_returns_none():
    """`Bearer    ` (only whitespace after prefix) -> None (after strip)."""
    db = _StubDB({})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    assert await r.resolve_bearer("Bearer    ") is None
    assert db.lookups == 0


async def test_wrong_case_bearer_prefix_rejected():
    """`bearer foo` (lowercase) must not match — code uses startswith("Bearer ")."""
    db = _StubDB({})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    assert await r.resolve_bearer("bearer foo") is None
    assert db.lookups == 0


async def test_unicode_bearer_hashes_consistently():
    """A unicode key is UTF-8 encoded and produces a valid v2:hmac-sha256: prefix.

    Also verifies the hash round-trips through the stub DB lookup.
    """
    raw = "héllo"
    h = hash_api_key(raw, pepper=_TEST_PEPPER)
    assert h.startswith("v2:hmac-sha256:")
    assert len(h) == len("v2:hmac-sha256:") + 64
    # Hex chars only after the prefix.
    int(h[len("v2:hmac-sha256:"):], 16)

    db = _StubDB({h: Caller(name="svc-u", daily_token_cap=10, enabled=True)})
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    caller = await r.resolve_bearer(f"Bearer {raw}")
    assert caller is not None
    assert caller.name == "svc-u"


async def test_db_exception_propagates():
    """If the DB raises, the exception propagates and nothing is cached."""

    class _RaisingDB:
        def __init__(self) -> None:
            self.lookups = 0

        async def caller_by_key_hash(self, key_hash: str):
            self.lookups += 1
            raise RuntimeError("db down")

    db = _RaisingDB()
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0)
    with pytest.raises(RuntimeError, match="db down"):
        await r.resolve_bearer("Bearer some-key")
    # Nothing should have been cached.
    assert len(r._cache) == 0


async def test_ttl_boundary_just_inside_and_just_outside():
    """Cache uses strict `<` so the cache expires at exactly TTL.

    - At `t == cache_ttl_s` (exactly at the boundary), the cache entry has
      expired and a fresh DB lookup occurs (lookups == 2).
    - At `t == cache_ttl_s - epsilon`, the entry is still fresh (lookups == 1).
    """
    key = "secret-bdy"
    caller = Caller(name="svc-a", daily_token_cap=1000, enabled=True)
    # Just-outside (== TTL): expires
    db_out = _StubDB({hash_api_key(key, pepper=_TEST_PEPPER): caller})
    clock_out = [0.0]
    r_out = CallerResolver(db=db_out, pepper=_TEST_PEPPER, cache_ttl_s=60.0, now_s_fn=lambda: clock_out[0])
    await r_out.resolve_bearer(f"Bearer {key}")
    assert db_out.lookups == 1
    clock_out[0] = 60.0  # exactly at the boundary
    await r_out.resolve_bearer(f"Bearer {key}")
    assert db_out.lookups == 2

    # Just-inside (< TTL): still cached
    db_in = _StubDB({hash_api_key(key, pepper=_TEST_PEPPER): caller})
    clock_in = [0.0]
    r_in = CallerResolver(db=db_in, pepper=_TEST_PEPPER, cache_ttl_s=60.0, now_s_fn=lambda: clock_in[0])
    await r_in.resolve_bearer(f"Bearer {key}")
    assert db_in.lookups == 1
    clock_in[0] = 59.999
    await r_in.resolve_bearer(f"Bearer {key}")
    assert db_in.lookups == 1


async def test_unbounded_cache_growth_documents_cr1_3_4():
    """Document the cache-bound contract from cr-1 §3.4.

    With the bound much larger than the burst, all unique invalid bearers stay
    cached. TODO(cr-1 §3.4): once auth cache is bounded by default to a small
    MAX, this assertion should be `<= MAX`.
    """
    db = _StubDB({})
    # Set the maxsize well above the burst so we observe full retention.
    r = CallerResolver(db=db, pepper=_TEST_PEPPER, cache_ttl_s=60.0, cache_maxsize=10_000)
    for i in range(1000):
        await r.resolve_bearer(f"Bearer flood-{i}")
    assert len(r._cache) == 1000


async def test_resolve_bearer_rejects_when_pepper_mismatched():
    """Seed DB with a hash under pepper-A; init resolver with pepper-B.

    The hash lookup fails (different hash) and resolve_bearer returns None —
    the caller gets a 401 as if the key does not exist.
    """
    key = "secret-key"
    pepper_a = "pepper-aaaaaa"
    pepper_b = "pepper-bbbbbb"

    # Store the hash computed under pepper_a.
    db = _StubDB(
        {hash_api_key(key, pepper=pepper_a): Caller(name="svc-mismatch", daily_token_cap=100, enabled=True)}
    )
    # Resolver uses pepper_b — the computed hash will differ.
    r = CallerResolver(db=db, pepper=pepper_b, cache_ttl_s=60.0)
    result = await r.resolve_bearer(f"Bearer {key}")
    assert result is None


async def test_caller_resolver_requires_non_empty_pepper():
    """CallerResolver.__init__ must reject a falsy pepper at construction time."""
    db = _StubDB({})
    with pytest.raises(ValueError, match="GATEWAY_KEY_HASH_PEPPER"):
        CallerResolver(db=db, pepper="", cache_ttl_s=60.0)
