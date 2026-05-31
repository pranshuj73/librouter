"""Tests for scripts/seed_config.py.

TDD sequence — tests are written before their production counterparts.

Uses testcontainers Postgres. Skipped when Docker is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

docker = pytest.importorskip("docker")
try:
    _client = docker.from_env()
    _client.ping()
except Exception:
    pytest.skip("docker daemon not available", allow_module_level=True)

from testcontainers.postgres import PostgresContainer  # noqa: E402
from fakeredis import aioredis as fakeredis_aio  # noqa: E402

from gateway.db import Database  # noqa: E402
from gateway.redis_state import RedisState  # noqa: E402
from gateway.config_store import ConfigStore  # noqa: E402


pytestmark = pytest.mark.asyncio


_SAMPLE_YAML = {
    "provider_mode": "mock",
    "secrets_mode": "mock",
    "tiers": {
        "fast": {
            "candidates": [
                {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "weight": 50,
                    "rate_limits": {"rpm": 1000, "tpm": 100000},
                },
                {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                    "weight": 30,
                    "rate_limits": {"rpm": 1000, "tpm": 100000},
                },
            ]
        },
        "smart": {
            "candidates": [
                {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "weight": 60,
                    "rate_limits": {"rpm": 500, "tpm": 50000},
                }
            ]
        },
    },
    "routing": {
        "refresh_interval_ms": 1000,
        "health_window_s": 60,
        "target_latency_s": 3.0,
        "min_weight_floor": 0.02,
        "rng_seed_env": "GATEWAY_RNG_SEED",
    },
    "callers": [],
}


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        raw = (
            pg.get_connection_url()
            .replace("+psycopg2", "")
            .replace("postgresql", "postgres")
        )
        yield raw


@pytest.fixture
async def db(pg_dsn):
    d = Database(dsn=pg_dsn)
    await d.connect()
    async with d.pool.acquire() as c:
        await c.execute("DROP TABLE IF EXISTS routing_config")
        await c.execute("DROP TABLE IF EXISTS tier_models")
        await c.execute("DROP TABLE IF EXISTS tiers")
        await c.execute("DROP TABLE IF EXISTS requests")
        await c.execute("DROP TABLE IF EXISTS callers")
    await d.run_migrations()
    yield d
    await d.close()


@pytest.fixture
async def store(db):
    r = fakeredis_aio.FakeRedis(decode_responses=False)
    state = RedisState(r)
    await state.load_scripts()
    try:
        yield ConfigStore(db=db, redis_state=state, cache_ttl_s=60)
    finally:
        await r.flushall()
        await r.aclose()


@pytest.fixture
def yaml_file(tmp_path):
    p = tmp_path / "config-seeding.yaml"
    p.write_text(yaml.safe_dump(_SAMPLE_YAML))
    return p


# ---------------------------------------------------------------- Step 1: seed_from_yaml populates all tables


async def test_seed_from_yaml_writes_full_config(store, yaml_file, db):
    """seed_from_yaml reads a YAML file and writes all three DB tables."""
    from scripts.seed_config import seed_from_yaml

    await seed_from_yaml(yaml_file, store)

    # Verify routing_config was written.
    routing = await db.fetch_routing_config()
    assert routing is not None
    assert routing["refresh_interval_ms"] == 1000
    assert routing["health_window_s"] == 60

    # Verify tiers table has our two tiers.
    tiers = await db.fetch_tiers()
    tier_names = {t["name"] for t in tiers}
    assert "fast" in tier_names
    assert "smart" in tier_names

    # Verify tier_models has openai and anthropic providers.
    tier_models = await db.fetch_tier_models()
    providers = {r["provider"] for r in tier_models}
    assert "openai" in providers
    assert "anthropic" in providers

    # Check rate_limits nested inside the JSONB config.
    openai_row = next(r for r in tier_models if r["provider"] == "openai")
    assert "fast" in openai_row["config"]
    assert openai_row["config"]["fast"]["rate_limits"]["rpm"] == 1000


# ---------------------------------------------------------------- Step 2: seed_from_yaml is idempotent


async def test_seed_from_yaml_idempotent(store, yaml_file, db):
    """Calling seed_from_yaml twice produces the same final state."""
    from scripts.seed_config import seed_from_yaml

    await seed_from_yaml(yaml_file, store)
    await seed_from_yaml(yaml_file, store)

    routing = await db.fetch_routing_config()
    assert routing is not None
    assert routing["refresh_interval_ms"] == 1000

    tiers = await db.fetch_tiers()
    assert len(tiers) == 2

    tier_models = await db.fetch_tier_models()
    assert len(tier_models) == 2  # openai and anthropic (smart only has openai)
