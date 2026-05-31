"""Tests for gateway/config_store.py.

TDD sequence — each test is written (and initially fails) before the
corresponding production code is added.

Uses testcontainers Postgres + fakeredis so this suite runs without
docker for the Redis side, but needs Docker for Postgres.
"""

from __future__ import annotations

import json
import copy

import pytest

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
from gateway.config_store import ConfigStore, ConfigStoreError  # noqa: E402
from gateway.models import Config  # noqa: E402


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- shared fixtures


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
    # Fresh schema for each test.
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
async def fake_redis():
    r = fakeredis_aio.FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.flushall()
        await r.aclose()


@pytest.fixture
async def store(db, fake_redis):
    state = RedisState(fake_redis)
    await state.load_scripts()
    return ConfigStore(db=db, redis_state=state, cache_ttl_s=60)


def _minimal_config() -> Config:
    """A small but fully-valid Config in the new schema (no top-level prices/rate_limits)."""
    return Config.model_validate(
        {
            "provider_mode": "mock",
            "secrets_mode": "mock",
            "tiers": {
                "fast": {
                    "candidates": [
                        {
                            "provider": "openai",
                            "model": "gpt-4o-mini",
                            "weight": 50.0,
                            "rate_limits": {"rpm": 1000, "tpm": 100000},
                        },
                        {
                            "provider": "anthropic",
                            "model": "claude-haiku-4-5",
                            "weight": 30.0,
                            "rate_limits": {"rpm": 1000, "tpm": 100000},
                        },
                    ],
                },
                "smart": {
                    "candidates": [
                        {
                            "provider": "openai",
                            "model": "gpt-4o",
                            "weight": 60.0,
                            "rate_limits": {"rpm": 500, "tpm": 50000},
                        },
                    ],
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
    )


# ---------------------------------------------------------------- Step 1: empty tables raise


async def test_load_from_db_raises_when_tables_empty(store):
    """load_from_db must raise ConfigStoreError when routing_config has no row."""
    with pytest.raises(ConfigStoreError, match="routing_config"):
        await store.load_from_db()


# ---------------------------------------------------------------- Step 2: write → load round-trip


async def test_write_then_load_round_trip(store):
    """write() followed by load_from_db() returns a Config equal to the original."""
    cfg = _minimal_config()
    await store.write(cfg)

    loaded = await store.load_from_db()

    assert loaded.provider_mode == cfg.provider_mode
    assert loaded.secrets_mode == cfg.secrets_mode
    assert set(loaded.tiers.keys()) == set(cfg.tiers.keys())

    for tier_name, tier_cfg in cfg.tiers.items():
        loaded_tier = loaded.tiers[tier_name]
        assert len(loaded_tier.candidates) == len(tier_cfg.candidates)
        # Order across providers is not guaranteed; compare as sets of tuples.
        orig_set = {
            (c.provider, c.model, c.weight, c.rate_limits.rpm, c.rate_limits.tpm)
            for c in tier_cfg.candidates
        }
        loaded_set = {
            (c.provider, c.model, c.weight, c.rate_limits.rpm, c.rate_limits.tpm)
            for c in loaded_tier.candidates
        }
        assert loaded_set == orig_set

    assert loaded.routing.refresh_interval_ms == cfg.routing.refresh_interval_ms
    assert loaded.routing.health_window_s == cfg.routing.health_window_s
    assert float(loaded.routing.target_latency_s) == float(cfg.routing.target_latency_s)
    assert float(loaded.routing.min_weight_floor) == float(cfg.routing.min_weight_floor)
    assert loaded.routing.rng_seed_env == cfg.routing.rng_seed_env


# ---------------------------------------------------------------- Step 3: Redis cache hit


async def test_load_or_refresh_hits_redis_cache(store, db, fake_redis):
    """Second call to load_or_refresh returns Redis-cached value (no DB read for routing_config)."""
    cfg = _minimal_config()
    await store.write(cfg)

    # First call — DB hit + write to Redis.
    first = await store.load_or_refresh()
    assert first is not None

    # Corrupt the DB routing_config row so we can detect a cache miss.
    async with db.pool.acquire() as c:
        await c.execute("DELETE FROM routing_config")

    # Second call without force — should still work from Redis cache.
    second = await store.load_or_refresh()
    assert second is not None
    assert second.routing.refresh_interval_ms == first.routing.refresh_interval_ms


# ---------------------------------------------------------------- Step 4: force bypasses cache


async def test_load_or_refresh_force_bypasses_cache(store, db, fake_redis):
    """force=True re-fetches from DB even when Redis has a cached value."""
    cfg = _minimal_config()
    await store.write(cfg)

    # Prime the cache.
    first = await store.load_or_refresh()

    # Now write a second config out-of-band (upsert directly).
    async with db.pool.acquire() as c:
        await c.execute(
            "UPDATE routing_config SET refresh_interval_ms = 9999 WHERE id = 1"
        )

    # Without force: cache is returned (old interval).
    cached = await store.load_or_refresh(force=False)
    assert cached.routing.refresh_interval_ms == first.routing.refresh_interval_ms

    # With force: fresh DB read.
    fresh = await store.load_or_refresh(force=True)
    assert fresh.routing.refresh_interval_ms == 9999


# ---------------------------------------------------------------- Step 5: write invalidates cache


async def test_write_invalidates_redis_cache(store, fake_redis):
    """write() deletes the Redis cache key so next load_or_refresh re-fetches from DB."""
    cfg = _minimal_config()
    await store.write(cfg)

    # Prime the cache.
    await store.load_or_refresh()

    # Confirm key is present.
    key_before = await fake_redis.exists(b"gw:config:current")
    assert key_before == 1

    # Write again — must invalidate cache.
    await store.write(cfg)

    key_after = await fake_redis.exists(b"gw:config:current")
    assert key_after == 0
