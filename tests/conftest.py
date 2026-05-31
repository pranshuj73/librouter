"""Shared pytest fixtures.

We use `fakeredis` (with the `lua` extra) for unit-level Redis tests so the
suite runs without Docker and finishes in milliseconds. The e2e test in step
14 still uses a real testcontainers Redis to catch any divergence.
"""

from __future__ import annotations

import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio


@pytest_asyncio.fixture
async def redis():
    """A clean fakeredis async client per test."""
    r = fakeredis_aio.FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.flushall()
        await r.aclose()
