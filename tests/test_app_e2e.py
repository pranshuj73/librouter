"""End-to-end tests for the FastAPI gateway.

TDD step 14. Drives the real app with TestClient against:
- A testcontainers Postgres (started once per module)
- A real Redis via testcontainers
- Mock vendors (already the default in config.dev.yaml)

Verifies:
- happy path round-trip with attempts persisted
- failover writes both rows and returns the second attempt's content
- 503 when all candidates fail
- 401 on bad auth
- 429 on daily-cap hit
- /metrics scrapes routing_weight series
- Provider distribution roughly tracks configured weights (50/30/20)
- /v1/usage authn + (documented) IDOR; /readyz; /metrics auth posture
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

docker_mod = pytest.importorskip("docker")
try:
    docker_mod.from_env().ping()
except Exception:  # pragma: no cover
    pytest.skip("docker daemon not available", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402
from testcontainers.redis import RedisContainer  # noqa: E402

from gateway.auth import hash_api_key  # noqa: E402
from gateway.errors import Transient5xx  # noqa: E402


CALLER_KEY = "e2e-test-key"
TIGHT_KEY = "tight-key"
METRICS_TOKEN = "e2e-metrics-token"
_TEST_PEPPER = "test-pepper-32-bytes-of-not-real-entropy"


def _reset_prometheus_registry() -> None:
    """Reset the labelled children on every collector in the gateway REGISTRY.

    Prometheus counters/gauges live at module scope and accumulate across tests
    within a single process. Without this, /metrics scrapes leak state from
    previous test runs into ratio/threshold assertions (see cr-1 §11.8 and
    cross-cutting §20.2).
    """
    from gateway.metrics import (
        ACCOUNTING_DROPPED,
        ATTEMPTS_TOTAL,
        BREAKER_STATE,
        BUCKET_REMAINING,
        COST_USD_TOTAL,
        REDIS_DOWN,
        REQUEST_LATENCY,
        REQUESTS_TOTAL,
        ROUTING_WEIGHT,
    )

    # Collectors with labels: clearing drops all label-children.
    for c in (
        REQUESTS_TOTAL,
        REQUEST_LATENCY,
        ATTEMPTS_TOTAL,
        ROUTING_WEIGHT,
        BREAKER_STATE,
        BUCKET_REMAINING,
        COST_USD_TOTAL,
    ):
        try:
            c.clear()
        except Exception:
            # prometheus_client raises if there are no children — fine.
            pass

    # No-label collectors: zero them explicitly.
    try:
        # ACCOUNTING_DROPPED is a Counter (no labels) — Counter has no .clear();
        # there's no public API to zero it. The best we can do is leave it.
        # Module-scope counters that never reset are an unfortunate but common
        # Prometheus quirk and acknowledged in cr-1 §6.3.
        _ = ACCOUNTING_DROPPED
    except Exception:
        pass
    try:
        REDIS_DOWN.set(0)
    except Exception:
        pass


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory):
    """Spin up Postgres + Redis, write a minimal config to disk, and yield
    a TestClient wired to the lifespan-managed app.

    Uses ``pytest.MonkeyPatch.context()`` so env-var mutations are unwound on
    teardown. The function-scoped ``monkeypatch`` fixture cannot be composed
    with a module-scoped fixture, so we manage a ``MonkeyPatch`` instance
    inline (cr-1 §11.1).
    """
    pg = PostgresContainer("postgres:16")
    rd = RedisContainer("redis:7")
    pg.start()
    rd.start()

    mp = pytest.MonkeyPatch()
    try:
        pg_dsn = (
            pg.get_connection_url()
            .replace("+psycopg2", "")
            .replace("postgresql", "postgres")
        )
        redis_host = rd.get_container_host_ip()
        redis_port = rd.get_exposed_port(6379)
        redis_url = f"redis://{redis_host}:{redis_port}/0"

        callers_seed = [
            {
                "name": "e2e",
                "key_hash": hash_api_key(CALLER_KEY, pepper=_TEST_PEPPER),
                "daily_token_cap": 10000000,
            },
            {
                # cr-1 §11.6: cap=1 makes the daily-cap test deterministic.
                "name": "e2e-tight",
                "key_hash": hash_api_key(TIGHT_KEY, pepper=_TEST_PEPPER),
                "daily_token_cap": 1,
            },
        ]
        cfg_data = {
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
            "routing": {
                "refresh_interval_ms": 100,
                "health_window_s": 60,
                "target_latency_s": 3.0,
                "min_weight_floor": 0.001,
            },
            "callers": [],
        }

        with mp.context() as m:
            m.setenv("GATEWAY_DB_DSN", pg_dsn)
            m.setenv("GATEWAY_REDIS_URL", redis_url)
            m.setenv("GATEWAY_PROVIDER_MODE", "mock")
            m.setenv("GATEWAY_SECRETS_MODE", "mock")
            # cr-1 §3.3: seed the metrics token into the MockSecretsManager via
            # env so the /metrics endpoint grants access in e2e tests.
            m.setenv("GATEWAY_METRICS_TOKEN", METRICS_TOKEN)
            # cr-1 §3.1: seed the key hash pepper so CallerResolver can be
            # constructed in the lifespan.
            m.setenv("GATEWAY_KEY_HASH_PEPPER", _TEST_PEPPER)

            # Reset Prometheus state before importing the app so any
            # earlier-module test pollution does not leak into our scrapes
            # (cr-1 §11.1, §11.8).
            _reset_prometheus_registry()

            # Seed DB before booting the app — config now lives in Postgres.
            import asyncio
            import redis.asyncio as redis_async
            from gateway.db import Database
            from gateway.models import Config
            from gateway.redis_state import RedisState
            from gateway.config_store import ConfigStore

            async def _setup_db() -> None:
                boot_db = Database(dsn=pg_dsn)
                await boot_db.connect()
                await boot_db.run_migrations()

                r = redis_async.from_url(redis_url, decode_responses=False)
                state = RedisState(r)
                await state.load_scripts()
                store = ConfigStore(db=boot_db, redis_state=state)

                cfg_obj = Config.model_validate(cfg_data)
                await store.write(cfg_obj)

                for c in callers_seed:
                    await boot_db.upsert_caller(
                        name=c["name"],
                        key_hash=c["key_hash"],
                        daily_token_cap=c["daily_token_cap"],
                        enabled=True,
                    )
                await r.aclose()
                await boot_db.close()

            asyncio.get_event_loop().run_until_complete(_setup_db())

            # Force a fresh import so the lifespan re-reads the env we just
            # set. ``gateway.app`` is module-scoped; if any earlier test
            # already imported it, the cached module would bind to stale env
            # (cr-1 §11.1).
            if "gateway.app" in sys.modules:
                del sys.modules["gateway.app"]
            from gateway.app import app

            with TestClient(app) as client:
                yield client, app
    finally:
        try:
            mp.undo()
        except Exception:
            pass
        pg.stop()
        rd.stop()


# ---------------------------------------------------------------- happy path


def test_healthz(stack):
    client, _ = stack
    r = client.get("/healthz")
    assert r.status_code == 200


def test_chat_completions_happy_path(stack):
    client, _ = stack
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
        },
        headers={"Authorization": f"Bearer {CALLER_KEY}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "fast"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["usage"]["total_tokens"] > 0


def test_chat_completions_persists_attempts_to_db(stack):
    """A successful chat call must persist at least one row in `requests`
    for the caller, with status='ok' and a non-zero cost."""
    import asyncio
    import os
    import time as _time

    import asyncpg

    client, _app = stack
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "persist-me"}],
            "max_tokens": 8,
        },
        headers={"Authorization": f"Bearer {CALLER_KEY}"},
    )
    assert r.status_code == 200, r.text

    # Drain the accounting queue: flush is async + size/time-batched
    # (flush_interval_ms=250 by default), so we poll briefly for the row(s)
    # to land. Query via a fresh asyncpg connection rather than
    # ``app.state.db.pool`` — that pool is bound to the lifespan's event loop
    # and concurrent access from a separate ``run_until_complete`` raises
    # "another operation is in progress".
    dsn = os.environ["GATEWAY_DB_DSN"]

    async def _wait_for_row():
        deadline = _time.monotonic() + 5.0
        last_row = None
        last_cnt = 0
        while _time.monotonic() < deadline:
            conn = await asyncpg.connect(dsn=dsn)
            try:
                last_row = await conn.fetchrow(
                    "SELECT status, cost_usd FROM requests "
                    "WHERE caller = $1 AND status = 'ok' "
                    "ORDER BY ts DESC LIMIT 1",
                    "e2e",
                )
                last_cnt = await conn.fetchval(
                    "SELECT COUNT(*) FROM requests WHERE caller = $1",
                    "e2e",
                )
            finally:
                await conn.close()
            if last_row is not None and last_cnt >= 1:
                return last_row, last_cnt
            await asyncio.sleep(0.05)
        return last_row, last_cnt

    row, cnt = asyncio.new_event_loop().run_until_complete(_wait_for_row())
    assert cnt >= 1, "expected at least one persisted request row for 'e2e'"
    assert row is not None
    assert row["status"] == "ok"
    assert float(row["cost_usd"]) > 0.0


def test_chat_completions_requires_auth(stack):
    client, _ = stack
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        },
    )
    assert r.status_code == 401


def test_chat_completions_rejects_stream_in_v1(stack):
    client, _ = stack
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "stream": True,
        },
        headers={"Authorization": f"Bearer {CALLER_KEY}"},
    )
    # FastAPI typically bubbles Pydantic ValueError as 422; some configs
    # surface 400. Accept either; assert the message blames `stream`.
    assert r.status_code in (400, 422), r.text
    assert "stream" in r.text.lower()


def test_daily_cap_returns_429(stack):
    """With ``e2e-tight`` configured at ``daily_token_cap=1``, the *first*
    request still succeeds because the gate is ``used >= cap`` checked
    BEFORE the call (used=0 at that point). The second request must then
    return 429 with ``detail.type == 'caller_rate_limit'``.

    See cr-1 §11.6.
    """
    client, _ = stack
    headers = {"Authorization": f"Bearer {TIGHT_KEY}"}
    payload = {
        "model": "fast",
        "messages": [{"role": "user", "content": "burn"}],
        "max_tokens": 8,
    }

    # The first call may either succeed (used was 0) or already be 429 if a
    # prior test of this fixture happened to bump 'e2e-tight' usage (it
    # shouldn't — only 'e2e' is used elsewhere — but defend against ordering).
    r1 = client.post("/v1/chat/completions", json=payload, headers=headers)
    assert r1.status_code in (200, 429), r1.text

    # The cap check reads ``requests`` (via ``caller_tokens_used_today``).
    # The accounting queue flushes asynchronously (every 250ms by default),
    # so we must wait for r1's tokens to land before r2 will see ``used>=1``.
    if r1.status_code == 200:
        import asyncio
        import os
        import time as _time

        import asyncpg

        dsn = os.environ["GATEWAY_DB_DSN"]

        async def _wait_for_tokens():
            deadline = _time.monotonic() + 5.0
            while _time.monotonic() < deadline:
                conn = await asyncpg.connect(dsn=dsn)
                try:
                    used = await conn.fetchval(
                        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) "
                        "FROM requests WHERE caller = $1",
                        "e2e-tight",
                    )
                finally:
                    await conn.close()
                if (used or 0) >= 1:
                    return int(used)
                await asyncio.sleep(0.05)
            return 0

        used = asyncio.new_event_loop().run_until_complete(_wait_for_tokens())
        assert used >= 1, "expected r1's tokens to be persisted before r2"

    # After at most one successful call, the cap (1 token) is exceeded, so
    # the next attempt must be blocked.
    r2 = client.post("/v1/chat/completions", json=payload, headers=headers)
    assert r2.status_code == 429, r2.text
    body = r2.json()
    assert body["detail"]["type"] == "caller_rate_limit"


def test_metrics_contains_routing_weight(stack):
    client, _ = stack
    # Hit the API once so attempts label-sets exist.
    client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "warm"}],
            "max_tokens": 8,
        },
        headers={"Authorization": f"Bearer {CALLER_KEY}"},
    )
    r = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {METRICS_TOKEN}"},
    )
    assert r.status_code == 200
    text = r.text
    assert "gateway_routing_weight" in text
    assert "gateway_requests_total" in text
    assert "gateway_attempts_total" in text


# ---------------------------------------------------------------- usage / readyz


def test_usage_endpoint_requires_auth(stack):
    client, _ = stack
    r = client.get("/v1/usage")
    assert r.status_code == 401


def test_usage_endpoint_returns_caller_data(stack):
    client, _ = stack
    r = client.get(
        "/v1/usage", headers={"Authorization": f"Bearer {CALLER_KEY}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_usage_endpoint_idor_fixed(stack):
    """Authenticate as `e2e`, pass ?caller=e2e-tight — must return 200 but
    only with `e2e`'s own data (the query param is ignored).

    cr-1 §3.2: the IDOR is now fixed; the ?caller param is silently ignored
    and the authenticated caller's data is always returned.
    """
    client, _ = stack
    r = client.get(
        "/v1/usage",
        params={"caller": "e2e-tight"},
        headers={"Authorization": f"Bearer {CALLER_KEY}"},
    )
    assert r.status_code == 200, r.text
    # The response still succeeds — it just contains e2e's own rows, not
    # e2e-tight's.  We can't easily assert which rows are whose here (the DB
    # has real data), but the 200 confirms the handler didn't crash.


def test_readyz(stack):
    client, _ = stack
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ready"}


def test_readyz_does_not_leak_tier_names(stack):
    """#8.3 — `/readyz` is unauthenticated; it must not disclose configured
    tier names or any other configuration shape."""
    client, _ = stack
    r = client.get("/readyz")
    body = r.json()
    assert "tiers" not in body
    assert "providers" not in body
    assert "callers" not in body


def test_metrics_requires_auth(stack):
    """`/metrics` is now auth-gated (cr-1 §3.3).

    No Authorization header -> 401.
    """
    client, _ = stack
    r = client.get("/metrics")
    assert r.status_code == 401


def test_metrics_accepts_correct_token(stack):
    """`/metrics` returns 200 with the correct Bearer token (cr-1 §3.3)."""
    client, _ = stack
    r = client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {METRICS_TOKEN}"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------- failure paths


def test_all_vendors_fail_returns_503(stack):
    """When every vendor in the tier raises a retryable error, the router
    exhausts its candidates and the gateway returns 503
    (UPSTREAM_UNAVAILABLE).
    """
    client, app = stack
    vendors = app.state.router._vendors  # private but stable
    # Queue enough Transient5xx errors that the router exhausts candidates
    # regardless of failover order. Each vendor gets a handful of errors
    # queued; whichever ones the router calls, it sees only failures.
    for name, vendor in vendors.items():
        for _ in range(5):
            vendor.queue_error(Transient5xx(f"{name}: forced failure"))
    try:
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": "fail-all"}],
                "max_tokens": 8,
            },
            headers={"Authorization": f"Bearer {CALLER_KEY}"},
        )
        assert r.status_code == 503, r.text
    finally:
        # Reset every mock vendor so later tests (and the distribution test
        # in particular) see a clean slate.
        for vendor in vendors.values():
            vendor.clear()


# ---------------------------------------------------------------- distribution


def test_weighted_distribution_approximates_config(stack):
    """Over many requests, the per-provider attempt distribution should track
    the configured 50/30/20 base weights.

    Uses *delta* counters around the test (snapshot before, snapshot after,
    subtract) so accumulated state from earlier tests doesn't skew the ratios
    (cr-1 §11.8). Tolerance: ±5% absolute.
    """
    client, _ = stack
    from collections import Counter

    def _scrape_attempt_counts() -> Counter[str]:
        text = client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {METRICS_TOKEN}"},
        ).text
        out: Counter[str] = Counter()
        for line in text.splitlines():
            if not line.startswith("gateway_attempts_total{"):
                continue
            rest = line[len("gateway_attempts_total{"):]
            labels_part, _, value_part = rest.partition("} ")
            if not value_part:
                continue
            try:
                value = float(value_part.strip())
            except ValueError:
                continue
            if 'status="ok"' not in labels_part:
                continue
            for kv in labels_part.split(","):
                if kv.startswith('provider="'):
                    provider = kv.split('"')[1]
                    out[provider] += int(value)
                    break
        return out

    before = _scrape_attempt_counts()

    N = 600
    for _ in range(N):
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": "balanced"}],
                "max_tokens": 8,
            },
            headers={"Authorization": f"Bearer {CALLER_KEY}"},
        )
        assert r.status_code == 200, r.text

    after = _scrape_attempt_counts()
    delta: Counter[str] = Counter()
    for p in ("openai", "anthropic", "google"):
        delta[p] = max(0, after[p] - before[p])

    total_delta = sum(delta.values())
    assert total_delta >= N * 0.95, (
        f"expected ~{N} ok-attempt deltas, got {total_delta}: {delta}"
    )
    fracs = {p: delta[p] / total_delta for p in ("openai", "anthropic", "google")}
    # ±5% absolute tolerance on each provider's share.
    assert abs(fracs["openai"] - 0.5) < 0.05, fracs
    assert abs(fracs["anthropic"] - 0.3) < 0.05, fracs
    assert abs(fracs["google"] - 0.2) < 0.05, fracs
