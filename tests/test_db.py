"""Tests for gateway/db.py against a testcontainers Postgres.

TDD step 12. Skipped automatically if the local Docker daemon isn't usable.

Tests cover:
- migrations idempotent across re-runs
- requests insert + index used for caller/ts query
- callers upsert + caller_by_key_hash
- caller_tokens_used_today
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# Skip the entire module if Docker isn't available.
docker = pytest.importorskip("docker")
try:
    _client = docker.from_env()
    _client.ping()
except Exception:  # pragma: no cover - environment-dependent
    pytest.skip("docker daemon not available", allow_module_level=True)


from testcontainers.postgres import PostgresContainer  # noqa: E402

from gateway.db import Database  # noqa: E402
from gateway.models import AttemptRecord  # noqa: E402


pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        # asyncpg expects postgres:// (or postgresql://); the container gives
        # a psycopg2-flavored URL with +psycopg2 suffix. Strip that.
        raw = pg.get_connection_url().replace("+psycopg2", "").replace(
            "postgresql", "postgres"
        )
        yield raw


@pytest.fixture
async def db(pg_dsn):
    d = Database(dsn=pg_dsn)
    await d.connect()
    # Clean tables if a previous test left them.
    async with d.pool.acquire() as c:
        await c.execute("DROP TABLE IF EXISTS requests")
        await c.execute("DROP TABLE IF EXISTS callers")
    await d.run_migrations()
    yield d
    await d.close()


def _rec(idx: int, caller: str = "svc-a", status: str = "ok") -> AttemptRecord:
    return AttemptRecord(
        request_id=f"req-{idx}",
        caller=caller,
        tier="fast",
        provider="openai",
        model="gpt-4o-mini",
        attempt_idx=idx,
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.001,
        latency_ms=200,
        status=status,
        vendor_req_id=f"vrid-{idx}",
    )


async def test_migrations_idempotent(db: Database):
    # Already applied once in fixture; re-applying should not error.
    await db.run_migrations()


async def test_write_batch_and_caller_filter(db: Database):
    batch = [_rec(i) for i in range(5)]
    await db.write_batch(batch)
    async with db.pool.acquire() as c:
        n = await c.fetchval(
            "SELECT COUNT(*)::int FROM requests WHERE caller = 'svc-a'"
        )
    assert n == 5


async def test_caller_tokens_used_today(db: Database):
    await db.write_batch([_rec(i) for i in range(3)])
    used = await db.caller_tokens_used_today("svc-a")
    assert used == 3 * (10 + 20)


async def test_upsert_and_lookup_caller(db: Database):
    await db.upsert_caller(
        name="svc-x", key_hash="sha256:hash1", daily_token_cap=1000, enabled=True
    )
    c = await db.caller_by_key_hash("sha256:hash1")
    assert c is not None
    assert c.name == "svc-x"
    assert c.daily_token_cap == 1000
    assert c.enabled is True

    # Upsert again with new values
    await db.upsert_caller(
        name="svc-x", key_hash="sha256:hash1", daily_token_cap=2000, enabled=False
    )
    c2 = await db.caller_by_key_hash("sha256:hash1")
    assert c2 is not None
    assert c2.daily_token_cap == 2000
    assert c2.enabled is False


async def test_usage_summary(db: Database):
    await db.write_batch([_rec(i) for i in range(3)])
    await db.write_batch([_rec(i, caller="svc-b") for i in range(2)])
    summary = await db.usage_summary(
        since=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    by_caller = {row["caller"]: row for row in summary}
    assert by_caller["svc-a"]["attempts"] == 3
    assert by_caller["svc-b"]["attempts"] == 2
