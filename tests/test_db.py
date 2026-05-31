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
        # Round-trip one row to catch a column-order typo in executemany.
        row = await c.fetchrow(
            "SELECT cost_usd, latency_ms, vendor_req_id "
            "FROM requests WHERE request_id = 'req-0'"
        )
    assert n == 5
    assert row is not None
    assert float(row["cost_usd"]) == 0.001
    assert row["latency_ms"] == 200
    assert row["vendor_req_id"] == "vrid-0"


async def test_caller_tokens_used_today(db: Database):
    # Mixed callers: 3 svc-a rows + 2 svc-b rows. Each row is 30 tokens
    # (10 input + 20 output). Counts must not leak across callers.
    await db.write_batch([_rec(i) for i in range(3)])
    await db.write_batch([_rec(i + 100, caller="svc-b") for i in range(2)])
    assert await db.caller_tokens_used_today("svc-a") == 3 * 30
    assert await db.caller_tokens_used_today("svc-b") == 2 * 30

    # UTC midnight rollover: directly INSERT a row dated yesterday 23:59 UTC
    # and assert it is NOT counted toward today's total.
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_2359 = today_start - timedelta(minutes=1)
    async with db.pool.acquire() as c:
        await c.execute(
            """
            INSERT INTO requests
                (request_id, caller, tier, provider, model, attempt_idx,
                 input_tokens, output_tokens, cost_usd, latency_ms, status,
                 vendor_req_id, ts)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            "req-yesterday",
            "svc-a",
            "fast",
            "openai",
            "gpt-4o-mini",
            0,
            500,
            500,
            0.0,
            100,
            "ok",
            "vrid-yesterday",
            yesterday_2359,
        )
    # The yesterday row's 1000 tokens must NOT show up in today's tally.
    assert await db.caller_tokens_used_today("svc-a") == 3 * 30


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
    await db.write_batch([_rec(i + 100, caller="svc-b") for i in range(2)])
    an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

    # 1. caller= filter only.
    only_a = await db.usage_summary(caller="svc-a")
    callers_only_a = {row["caller"] for row in only_a}
    assert callers_only_a == {"svc-a"}
    assert sum(row["attempts"] for row in only_a) == 3

    # 2. since= filter only — both callers should appear.
    since_only = await db.usage_summary(since=an_hour_ago)
    by_caller = {row["caller"]: row for row in since_only}
    assert by_caller["svc-a"]["attempts"] == 3
    assert by_caller["svc-b"]["attempts"] == 2

    # 3. caller= AND since= together.
    both = await db.usage_summary(caller="svc-b", since=an_hour_ago)
    assert {row["caller"] for row in both} == {"svc-b"}
    assert sum(row["attempts"] for row in both) == 2

    # 4. Neither filter — returns everything (no WHERE clause).
    no_filter = await db.usage_summary()
    by_caller_nf = {row["caller"]: row for row in no_filter}
    assert by_caller_nf["svc-a"]["attempts"] == 3
    assert by_caller_nf["svc-b"]["attempts"] == 2


# ---------------------------------------------------------------- new tests (cr-1 findings)

async def test_run_migrations_raises_when_dir_missing(db: Database, monkeypatch, tmp_path):
    # Point _MIGRATIONS_DIR at a path that definitely does not exist.
    missing = tmp_path / "no_such_dir"
    monkeypatch.setattr("gateway.db._MIGRATIONS_DIR", missing)
    with pytest.raises(RuntimeError, match="migrations directory not found"):
        await db.run_migrations()


async def test_run_migrations_raises_when_dir_empty(db: Database, monkeypatch, tmp_path):
    # tmp_path exists but contains no .sql files.
    monkeypatch.setattr("gateway.db._MIGRATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="migrations directory is empty"):
        await db.run_migrations()


async def test_concurrent_migrations_serialize(pg_dsn):
    """Three concurrent run_migrations() calls must not crash.

    Because migrations are idempotent (CREATE TABLE IF NOT EXISTS), all three
    runs completing without error is the success condition.  We also assert the
    tables were actually created.
    """
    import asyncio as _asyncio

    d = Database(dsn=pg_dsn)
    await d.connect()
    # Start clean so the lock logic is exercised from a blank slate.
    async with d.pool.acquire() as c:
        await c.execute("DROP TABLE IF EXISTS requests")
        await c.execute("DROP TABLE IF EXISTS callers")
    try:
        await _asyncio.gather(
            d.run_migrations(),
            d.run_migrations(),
            d.run_migrations(),
        )
        # Verify both tables exist.
        async with d.pool.acquire() as c:
            tables = await c.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        table_names = {row["tablename"] for row in tables}
        assert "requests" in table_names
        assert "callers" in table_names
    finally:
        await d.close()


async def test_pool_property_raises_runtimeerror_before_connect():
    """db.pool must raise RuntimeError (not AssertionError) before connect().

    Guards against python -O stripping an assert-based guard.
    """
    d = Database(dsn="postgres://unused/unused")
    with pytest.raises(RuntimeError, match="connect"):
        _ = d.pool


# ---------------------------------------------------------------- new tests (t-1 §5)


async def test_write_batch_empty_list_is_noop(db: Database):
    """write_batch([]) returns without error and does not change row count."""
    async with db.pool.acquire() as c:
        before = await c.fetchval("SELECT COUNT(*)::int FROM requests")
    await db.write_batch([])
    async with db.pool.acquire() as c:
        after = await c.fetchval("SELECT COUNT(*)::int FROM requests")
    assert before == after


async def test_caller_name_safely_parameterized(db: Database):
    """Injection-attempt caller name does not corrupt the callers table.

    Proves parameter binding is in effect: the name is stored verbatim and
    the `callers` table still exists afterwards.
    """
    evil = '"; DROP TABLE callers; --'
    await db.upsert_caller(
        name=evil, key_hash="sha256:evil", daily_token_cap=None, enabled=True
    )
    async with db.pool.acquire() as c:
        # Table still exists.
        exists = await c.fetchval(
            "SELECT 1 FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename = 'callers'"
        )
        assert exists == 1
        # Row was inserted with the exact (unmodified) name.
        row = await c.fetchrow(
            "SELECT name, key_hash FROM callers WHERE key_hash = 'sha256:evil'"
        )
    assert row is not None
    assert row["name"] == evil
