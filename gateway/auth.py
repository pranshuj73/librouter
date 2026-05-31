"""Bearer-key auth -> Caller lookup with 60s in-process cache.

Caller API keys are never stored in plaintext; the gateway only knows their
SHA-256 hashes (with a `sha256:` prefix). The `key_hash` in `config.yaml` and
the `callers.key_hash` column use the same scheme.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from gateway.models import Caller


def hash_api_key(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


class _DBProtocol(Protocol):
    async def caller_by_key_hash(self, key_hash: str) -> Caller | None: ...


class CallerResolver:
    """Maps `Authorization: Bearer <key>` -> Caller.

    Both positive and negative results are cached for `cache_ttl_s` seconds —
    the typical caller has a fixed key for years, so the cache absorbs ~100%
    of lookups under steady traffic.

    The cache is bounded to `cache_maxsize` entries using an LRU eviction
    policy (oldest-inserted entry is evicted when the limit is exceeded).
    This prevents memory exhaustion from clients spamming unique bearer tokens.
    """

    def __init__(
        self,
        *,
        db: _DBProtocol,
        cache_ttl_s: float = 60.0,
        cache_maxsize: int = 10_000,
        now_s_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._ttl = cache_ttl_s
        self._maxsize = cache_maxsize
        self._now = now_s_fn
        self._cache: OrderedDict[str, tuple[float, Caller | None]] = OrderedDict()

    async def resolve_bearer(self, header: str | None) -> Caller | None:
        if not header or not header.startswith("Bearer "):
            return None
        token = header[len("Bearer "):].strip()
        if not token:
            return None
        key_hash = hash_api_key(token)

        now = self._now()
        cached = self._cache.get(key_hash)
        if cached is not None and now - cached[0] < self._ttl:
            # Mark as recently used so LRU eviction preserves it.
            self._cache.move_to_end(key_hash)
            return cached[1]

        caller = await self._db.caller_by_key_hash(key_hash)
        if caller is not None and not caller.enabled:
            caller = None
        self._cache[key_hash] = (now, caller)
        # Move to end in case we just updated an existing stale entry.
        self._cache.move_to_end(key_hash)
        # Evict the oldest (least-recently-used) entry if over capacity.
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
        return caller
