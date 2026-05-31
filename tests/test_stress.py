"""Concurrency / race-condition invariants for the gateway.

Each test asserts a property that MUST hold under modest concurrent load.
Failures here are real bugs — not flakes — and warrant a fix commit.

Opt-in: these run only with `pytest -m stress`. They're excluded from the
default fast suite by `-m "not stress"`.

Smoke level: ~100 ops per test, ~1–3s wall-clock each. Heavy enough to
shake out races without making CI slow.
"""

from __future__ import annotations

import asyncio
import os
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

# Skip module if docker isn't available — several tests need real Redis/PG
docker_mod = pytest.importorskip("docker")
try:
    docker_mod.from_env().ping()
except Exception:  # pragma: no cover
    pytest.skip("docker daemon not available", allow_module_level=True)

import redis.asyncio as redis_async  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402
from testcontainers.redis import RedisContainer  # noqa: E402

from gateway.accounting import AccountingQueue  # noqa: E402
from gateway.auth import CallerResolver, hash_api_key  # noqa: E402
from gateway.breaker import BreakerSet, BreakerState  # noqa: E402
from gateway.config_store import ConfigStore  # noqa: E402
from gateway.db import Database  # noqa: E402
from gateway.errors import RateLimited  # noqa: E402
from gateway.models import (  # noqa: E402
    AttemptRecord,
    Caller,
    ChatCompletionRequest,
    Config,
    Message,
)
from gateway.providers.mock import (  # noqa: E402
    MockAnthropicVendor,
    MockGoogleVendor,
    MockOpenAIVendor,
)
from gateway.ratelimit import RedisTokenBucket  # noqa: E402
from gateway.redis_state import RedisState  # noqa: E402
from gateway.router import Router  # noqa: E402
from gateway.routing.observe import Observer  # noqa: E402
from gateway.routing.refresh import RefreshTask, build_signals  # noqa: E402
from gateway.routing.weights import WeightEngine  # noqa: E402
from gateway.secrets import MockSecretsManager  # noqa: E402


pytestmark = [pytest.mark.asyncio, pytest.mark.stress]

_PEPPER = "stress-test-pepper-not-real"


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def pg_container():
    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture(scope="module")
def rd_container():
    with RedisContainer("redis:7") as rd:
        yield rd


@pytest.fixture
async def real_redis(rd_container):
    """Real-Redis async client; flushed between tests."""
    host = rd_container.get_container_host_ip()
    port = rd_container.get_exposed_port(6379)
    r = redis_async.from_url(f"redis://{host}:{port}/0", decode_responses=False)
    await r.flushdb()
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture
async def real_db(pg_container):
    dsn = (
        pg_container.get_connection_url()
        .replace("+psycopg2", "")
        .replace("postgresql", "postgres")
    )
    d = Database(dsn=dsn)
    await d.connect()
    # Clean state — drop every gateway table so each test starts fresh.
    async with d.pool.acquire() as c:
        await c.execute("DROP TABLE IF EXISTS requests")
        await c.execute("DROP TABLE IF EXISTS callers")
        await c.execute("DROP TABLE IF EXISTS tier_models")
        await c.execute("DROP TABLE IF EXISTS routing_config")
        await c.execute("DROP TABLE IF EXISTS tiers")
    await d.run_migrations()
    try:
        yield d
    finally:
        await d.close()


def _stress_config() -> Config:
    return Config.model_validate(
        {
            "provider_mode": "mock",
            "secrets_mode": "mock",
            "tiers": {
                "fast": {
                    "candidates": [
                        {"provider": "openai", "model": "gpt-4o-mini", "weight": 50.0,
                         "rate_limits": {"rpm": 100000, "tpm": 10000000}},
                        {"provider": "anthropic", "model": "claude-haiku-4-5", "weight": 30.0,
                         "rate_limits": {"rpm": 100000, "tpm": 10000000}},
                        {"provider": "google", "model": "gemini-2.5-flash", "weight": 20.0,
                         "rate_limits": {"rpm": 100000, "tpm": 10000000}},
                    ],
                },
            },
            "routing": {"refresh_interval_ms": 100, "health_window_s": 60,
                        "target_latency_s": 3.0, "min_weight_floor": 0.001},
            "callers": [],
        }
    )


# =================================================================
# 1. Lua-backed token bucket — never oversubscribes under contention
# =================================================================


async def test_bucket_never_oversubscribes(real_redis):
    """Fire 200 concurrent try_acquire calls at a bucket with RPM=50.
    No more than 50 of them may succeed — the Lua script is the only
    serialization point and must hold under real-Redis concurrency."""
    state = RedisState(real_redis)
    await state.load_scripts()
    rb = RedisTokenBucket(
        state=state,
        limits={"openai/gpt-4o-mini": SimpleNamespace(rpm=50, tpm=1_000_000)},
        now_ms_fn=lambda: 0,  # frozen clock — no refill
    )

    async def acquire():
        ok, _, _ = await rb.try_acquire("openai", "gpt-4o-mini", request_tokens=1)
        return ok

    results = await asyncio.gather(*[acquire() for _ in range(200)])
    succeeded = sum(1 for r in results if r)
    assert succeeded == 50, f"oversubscription: {succeeded} acquires succeeded against RPM=50"


# =================================================================
# 2. BreakerSet snapshot — atomic swap holds under concurrent record/read
# =================================================================


async def test_breaker_snapshot_holds_under_concurrent_mutation(real_redis):
    """Concurrent record_*, state(), and refresh_snapshot() calls must not
    raise or expose half-built snapshots."""
    state = RedisState(real_redis)
    await state.load_scripts()
    bs = BreakerSet(state=state)

    async def record_ok():
        await bs.record_success("openai", "gpt-4o-mini")

    async def record_fail():
        await bs.record_failure("openai", "gpt-4o-mini")

    async def read_state():
        return await bs.state("openai", "gpt-4o-mini")

    async def refresh():
        await bs.refresh_snapshot()

    tasks: list = []
    for _ in range(40):
        tasks.append(record_ok())
        tasks.append(record_fail())
        tasks.append(read_state())
        tasks.append(refresh())
    # No exception → the test passes; we don't assert on the final state because
    # interleaving of 160 ops is racey, but the system must not crash.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    excs = [r for r in results if isinstance(r, Exception)]
    assert not excs, f"concurrent breaker ops raised: {excs[:3]}"
    # Sanity: state() always returns a valid enum value
    states_only = [r for r in results if isinstance(r, BreakerState)]
    assert all(s in (BreakerState.CLOSED, BreakerState.HALF_OPEN, BreakerState.OPEN)
               for s in states_only)


# =================================================================
# 3. RefreshTask backoff actually slows tick rate during failure
# =================================================================


async def test_refresh_loop_backs_off_against_real_redis(real_redis, monkeypatch):
    """With a 10ms base interval and a constantly-failing build_signals,
    a 1-second window must produce <30 ticks (no-backoff would yield ~100)
    AND the error counter must equal the tick count exactly."""
    state = RedisState(real_redis)
    await state.load_scripts()

    cfg = _stress_config()
    fast_cfg = cfg.model_copy(
        update={"routing": cfg.routing.model_copy(update={"refresh_interval_ms": 10})}
    )
    obs = Observer(state=state, window_s=60)
    bs = BreakerSet(state=state)
    rb = RedisTokenBucket(
        state=state,
        limits={
            f"{c.provider}/{c.model}": c.rate_limits
            for c in fast_cfg.tiers["fast"].candidates
        },
    )
    engine = WeightEngine(routing=fast_cfg.routing)

    import gateway.routing.refresh as refresh_mod
    from gateway.metrics import REFRESH_ERRORS_TOTAL

    call_count = {"n": 0}

    async def always_fail(*_a, **_k):
        call_count["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(refresh_mod, "build_signals", always_fail)

    start_errors = REFRESH_ERRORS_TOTAL._value.get()
    task = RefreshTask(
        config=fast_cfg, observer=obs, bucket=rb, breakers=bs, engine=engine
    )
    task.start()
    await asyncio.sleep(1.0)
    await task.stop()
    end_errors = REFRESH_ERRORS_TOTAL._value.get()

    # Backoff invariant
    assert call_count["n"] < 30, (
        f"backoff failed: {call_count['n']} ticks in 1s with 10ms base interval"
    )
    # Counter accounting: every failure increments exactly once
    assert end_errors - start_errors == call_count["n"], (
        f"counter mismatch: {end_errors - start_errors} increments vs {call_count['n']} failures"
    )


# =================================================================
# 4. Server-side request_ids are unique across concurrent routes
# =================================================================


async def test_router_request_ids_are_unique(real_redis):
    """100 concurrent route() calls must produce 100 distinct uuid4 hex ids
    in the response and in every attempt row."""
    state = RedisState(real_redis)
    await state.load_scripts()
    cfg = _stress_config()
    obs = Observer(state=state, window_s=60)
    bs = BreakerSet(state=state)
    rb = RedisTokenBucket(
        state=state,
        limits={
            f"{c.provider}/{c.model}": c.rate_limits
            for c in cfg.tiers["fast"].candidates
        },
    )
    engine = WeightEngine(routing=cfg.routing)
    engine.update_cache(await build_signals(cfg, obs, rb, bs))

    secrets = MockSecretsManager()
    vendors = {
        "openai": MockOpenAIVendor(secrets),
        "anthropic": MockAnthropicVendor(secrets),
        "google": MockGoogleVendor(secrets),
    }
    router = Router(
        config=cfg,
        vendors=vendors,
        weight_engine=engine,
        bucket=rb,
        observer=obs,
        rng=random.SystemRandom(),
    )
    caller = Caller(name="stress", enabled=True)
    req = ChatCompletionRequest(
        model="fast",
        messages=[Message(role="user", content="hi")],
        max_tokens=8,
    )

    results = await asyncio.gather(*[router.route(req, caller) for _ in range(100)])
    response_ids = [r.response.id for r in results]
    assert len(set(response_ids)) == 100, "duplicate server-side request_ids"
    # And: within each result, every attempt shares that same request_id
    for r in results:
        ids = {a.request_id for a in r.attempts}
        assert len(ids) == 1


# =================================================================
# 5. Auth cache stays bounded under unique-token spam
# =================================================================


async def test_auth_cache_bounded_under_unique_token_spam():
    """A misbehaving client sending 1000 unique invalid bearers must not
    grow the auth cache past the configured cap of 100."""
    class _StubDB:
        async def caller_by_key_hash(self, _h):
            return None

    resolver = CallerResolver(
        db=_StubDB(),
        cache_ttl_s=60.0,
        cache_maxsize=100,
        pepper=_PEPPER,
    )
    await asyncio.gather(
        *[resolver.resolve_bearer(f"Bearer unique-{i}") for i in range(1000)]
    )
    assert len(resolver._cache) == 100, (
        f"cache grew to {len(resolver._cache)}, expected 100"
    )


# =================================================================
# 6. ConfigStore: cold cache + 50 concurrent loads → ≤ small DB hit count
# =================================================================


async def test_config_store_concurrent_cold_cache_load(real_db, real_redis):
    """Many concurrent `load_or_refresh` calls hitting a cold cache should
    not stampede the DB. We don't enforce strict 1-DB-hit since Python
    asyncio doesn't serialize the gap between cache-miss and cache-write,
    but the count should be small — definitely not N."""
    state = RedisState(real_redis)
    await state.load_scripts()
    store = ConfigStore(db=real_db, redis_state=state)

    cfg = _stress_config()
    await store.write(cfg)

    # Force a cold cache for the read test
    await real_redis.delete("gw:config:current")

    db_hits = {"n": 0}
    real_load = store.load_from_db

    async def counting_load():
        db_hits["n"] += 1
        return await real_load()

    store.load_from_db = counting_load  # type: ignore[method-assign]

    results = await asyncio.gather(*[store.load_or_refresh() for _ in range(50)])
    assert all(r.provider_mode == "mock" for r in results), "config corrupted"
    # Allow up to 10 DB hits — async race between cache-check and cache-fill
    # means a small number is unavoidable without an explicit lock. Anything
    # close to 50 indicates a full stampede and a real bug.
    assert db_hits["n"] <= 10, (
        f"DB stampede: {db_hits['n']} hits across 50 concurrent loads "
        "(expected <= 10)"
    )


# =================================================================
# 7. AccountingQueue drop counter is accurate under sustained overflow
# =================================================================


async def test_accounting_drop_counter_accurate_under_overflow():
    """Capacity=10. Push 1000 records before any flush. Assert dropped == 990
    and final buffer size == 10."""

    class _SinkWriter:
        async def write_batch(self, _records):  # pragma: no cover - never flushes
            pass

    q = AccountingQueue(
        writer=_SinkWriter(),
        capacity=10,
        flush_size=10_000,        # never triggered
        flush_interval_ms=60_000,  # never triggered
    )
    # Don't start the drain — we want the buffer to overflow purely on enqueue.
    for i in range(1000):
        q.enqueue(AttemptRecord(
            request_id=f"req-{i}",
            caller="stress",
            tier="fast",
            provider="openai",
            model="gpt-4o-mini",
            attempt_idx=0,
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.0001,
            latency_ms=100,
            status="ok",
        ))
    assert q.dropped_total == 990, (
        f"expected 990 drops, got {q.dropped_total}"
    )
    assert len(q._buffer) == 10


# =================================================================
# 8. Probe lock — only one of N concurrent callers wins (real Redis)
# =================================================================


async def test_probe_lock_only_one_wins_against_real_redis(real_redis):
    """fakeredis serializes Redis commands; real Redis doesn't. This test
    asserts the SET NX EX atomicity holds when 50 coroutines race."""
    state = RedisState(real_redis)
    await state.load_scripts()
    key = state.breaker_probe_key("openai", "gpt-4o-mini")
    results = await asyncio.gather(
        *[state.acquire_probe_lock(key, holder=f"h{i}", ttl_s=10) for i in range(50)]
    )
    winners = [r for r in results if r is True]
    assert len(winners) == 1, f"{len(winners)} probe lock winners (expected 1)"
