"""Tests for gateway/auth.py.

TDD step 13. CallerResolver maps `Authorization: Bearer <key>` -> Caller via
sha256(key) lookup with a 60s in-process cache.
"""

from __future__ import annotations

import pytest

from gateway.auth import CallerResolver, hash_api_key
from gateway.models import Caller


pytestmark = pytest.mark.asyncio


class _StubDB:
    def __init__(self, by_hash: dict[str, Caller]) -> None:
        self._by_hash = by_hash
        self.lookups = 0

    async def caller_by_key_hash(self, key_hash: str) -> Caller | None:
        self.lookups += 1
        return self._by_hash.get(key_hash)


async def test_hash_format():
    h = hash_api_key("hello")
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


async def test_resolve_valid_key():
    key = "secret-123"
    db = _StubDB(
        {hash_api_key(key): Caller(name="svc-a", daily_token_cap=1000, enabled=True)}
    )
    r = CallerResolver(db=db, cache_ttl_s=60.0)
    caller = await r.resolve_bearer(f"Bearer {key}")
    assert caller is not None
    assert caller.name == "svc-a"


async def test_missing_header_returns_none():
    db = _StubDB({})
    r = CallerResolver(db=db, cache_ttl_s=60.0)
    assert await r.resolve_bearer(None) is None


async def test_wrong_scheme_returns_none():
    db = _StubDB({})
    r = CallerResolver(db=db, cache_ttl_s=60.0)
    assert await r.resolve_bearer("Basic abc") is None


async def test_unknown_key_returns_none():
    db = _StubDB({})
    r = CallerResolver(db=db, cache_ttl_s=60.0)
    assert await r.resolve_bearer("Bearer nope") is None


async def test_disabled_caller_returns_none():
    key = "secret-xyz"
    db = _StubDB(
        {hash_api_key(key): Caller(name="svc-z", daily_token_cap=100, enabled=False)}
    )
    r = CallerResolver(db=db, cache_ttl_s=60.0)
    assert await r.resolve_bearer(f"Bearer {key}") is None


async def test_cache_hits_avoid_db_lookup():
    key = "secret-abc"
    db = _StubDB(
        {hash_api_key(key): Caller(name="svc-a", daily_token_cap=1000, enabled=True)}
    )
    clock = [0.0]
    r = CallerResolver(db=db, cache_ttl_s=60.0, now_s_fn=lambda: clock[0])
    assert await r.resolve_bearer(f"Bearer {key}") is not None
    assert db.lookups == 1
    # Still inside TTL
    clock[0] = 30.0
    assert await r.resolve_bearer(f"Bearer {key}") is not None
    assert db.lookups == 1


async def test_cache_expires_after_ttl():
    key = "secret-abc"
    db = _StubDB(
        {hash_api_key(key): Caller(name="svc-a", daily_token_cap=1000, enabled=True)}
    )
    clock = [0.0]
    r = CallerResolver(db=db, cache_ttl_s=60.0, now_s_fn=lambda: clock[0])
    await r.resolve_bearer(f"Bearer {key}")
    clock[0] = 61.0
    await r.resolve_bearer(f"Bearer {key}")
    assert db.lookups == 2


async def test_negative_cache_after_ttl_too():
    db = _StubDB({})
    clock = [0.0]
    r = CallerResolver(db=db, cache_ttl_s=60.0, now_s_fn=lambda: clock[0])
    await r.resolve_bearer("Bearer nope")
    assert db.lookups == 1
    clock[0] = 30.0
    await r.resolve_bearer("Bearer nope")
    # Cached miss within TTL
    assert db.lookups == 1
    clock[0] = 90.0
    await r.resolve_bearer("Bearer nope")
    assert db.lookups == 2
