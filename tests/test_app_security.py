"""Security-focused tests for gateway/app.py endpoints.

Covers:
- /metrics auth gate (#3.3)
- /v1/usage IDOR fix (#3.2)
- DSN required in real mode (#2.3)

All tests run in-process using FastAPI's TestClient with minimal stub state —
no testcontainers, no real Redis or Postgres.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from gateway.models import Caller
from gateway.secrets import MockSecretsManager

# --------------------------------------------------------------------------- #
# Minimal stub helpers                                                          #
# --------------------------------------------------------------------------- #

_METRICS_TOKEN = "test-metrics-secret"
_CALLER_KEY = "caller-a-key"
_CALLER_NAME = "caller-a"


def _make_caller(name: str = _CALLER_NAME) -> Caller:
    return Caller(name=name, daily_token_cap=1_000_000, enabled=True)


def _make_stub_auth(caller: Caller | None = None):
    """Return a CallerResolver-like stub whose resolve_bearer always resolves."""
    stub = MagicMock()
    stub.resolve_bearer = AsyncMock(return_value=caller or _make_caller())
    return stub


def _make_stub_db(*, usage_rows: list[dict[str, Any]] | None = None):
    """Return a Database-like stub."""
    stub = MagicMock()
    stub.usage_summary = AsyncMock(return_value=usage_rows or [])
    stub.caller_tokens_used_today = AsyncMock(return_value=0)
    return stub


def _stub_secrets(*, metrics_token: str | None = _METRICS_TOKEN) -> MockSecretsManager:
    seed = {}
    if metrics_token is not None:
        seed["GATEWAY_METRICS_TOKEN"] = metrics_token
    return MockSecretsManager(seed=seed)


# --------------------------------------------------------------------------- #
# App fixture                                                                   #
# --------------------------------------------------------------------------- #


def _make_app(*, metrics_token: str | None = _METRICS_TOKEN, caller: Caller | None = None):
    """Build a FastAPI app with stub state, bypassing the real lifespan."""
    from fastapi import FastAPI

    stub_app = FastAPI()

    # Wire stub state directly — no lifespan needed.
    stub_app.state.secrets = _stub_secrets(metrics_token=metrics_token)
    stub_app.state.auth = _make_stub_auth(caller=caller or _make_caller())
    stub_app.state.db = _make_stub_db()

    # Wire a minimal ConfigHolder stub so /readyz and /metrics don't crash.
    cfg_stub = MagicMock()
    cfg_stub.value.tiers = {}
    cfg_stub.value.routing.target_latency_s = 3.0
    cfg_stub.value.routing.min_weight_floor = 0.001
    stub_app.state.cfg = cfg_stub

    engine_stub = MagicMock()
    engine_stub.signals_for = MagicMock(return_value=None)
    stub_app.state.engine = engine_stub

    # Mount the routes from the real app module onto our stub.
    import gateway.app as app_module

    stub_app.add_api_route("/metrics", app_module.metrics, methods=["GET"])
    stub_app.add_api_route("/v1/usage", app_module.v1_usage, methods=["GET"])

    return stub_app


# --------------------------------------------------------------------------- #
# /metrics auth tests                                                           #
# --------------------------------------------------------------------------- #


class TestMetricsAuth:
    def test_metrics_requires_token(self):
        """No Authorization header -> 401."""
        app = _make_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/metrics")
        assert r.status_code == 401
        assert r.json() == {"detail": "metrics auth required"}

    def test_metrics_rejects_bad_token(self):
        """Wrong token -> 401."""
        app = _make_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401
        assert r.json() == {"detail": "metrics auth required"}

    def test_metrics_accepts_correct_token(self):
        """Correct token -> 200, body contains gateway_ series."""
        app = _make_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/metrics",
                headers={"Authorization": f"Bearer {_METRICS_TOKEN}"},
            )
        assert r.status_code == 200
        # Prometheus text format always begins with a HELP or TYPE comment for
        # the gateway_ metrics.  Even with no label-children populated the
        # registry returns the collector names.
        assert "gateway_" in r.text

    def test_metrics_fails_closed_when_token_unset(self):
        """If GATEWAY_METRICS_TOKEN is not in the secrets manager -> 401.

        The gateway must never leave /metrics open simply because the token
        hasn't been configured.
        """
        app = _make_app(metrics_token=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/metrics", headers={"Authorization": "Bearer anything"})
        assert r.status_code == 401

    def test_metrics_uses_constant_time_compare(self):
        """Sanity-check that hmac.compare_digest is used in app.py."""
        import gateway.app as app_module

        source = inspect.getsource(app_module)
        assert "compare_digest" in source, (
            "Expected hmac.compare_digest in gateway/app.py for timing-safe comparison"
        )


# --------------------------------------------------------------------------- #
# /v1/usage IDOR tests                                                          #
# --------------------------------------------------------------------------- #


class TestUsageIDOR:
    def test_v1_usage_ignores_query_caller(self):
        """Auth as caller-a, pass ?caller=caller-b — must only query caller-a.

        The db.usage_summary stub records what caller name it was called with.
        We assert it was called with the authenticated caller's name, not the
        query param value.
        """
        captured: list[str] = []

        async def _fake_usage_summary(caller: str | None = None) -> list[dict]:
            captured.append(caller or "")
            return []

        app = _make_app()
        # Override stub to capture the argument.
        app.state.db.usage_summary = _fake_usage_summary

        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get(
                "/v1/usage",
                params={"caller": "caller-b"},
                headers={"Authorization": f"Bearer {_CALLER_KEY}"},
            )

        assert r.status_code == 200
        # usage_summary must have been called with the *authenticated* caller,
        # not the user-supplied query param.
        assert len(captured) == 1
        assert captured[0] == _CALLER_NAME, (
            f"Expected usage_summary called with '{_CALLER_NAME}', "
            f"but got '{captured[0]}' — IDOR not fixed"
        )

    def test_v1_usage_requires_auth(self):
        """No auth -> 401."""
        app = _make_app()
        # Stub auth to reject the call.
        app.state.auth.resolve_bearer = AsyncMock(return_value=None)

        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/v1/usage")
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# #2.3 DSN required in real mode                                               #
# --------------------------------------------------------------------------- #


class TestDsnRequired:
    def test_real_mode_without_dsn_raises_runtime_error(self, monkeypatch):
        """Starting the app with GATEWAY_PROVIDER_MODE=real and no GATEWAY_DB_DSN
        must raise RuntimeError during lifespan startup.

        Config is now loaded from DB, not YAML. The DSN check happens before
        the DB connection is attempted, so no real DB is needed for this test.
        """
        from fastapi.testclient import TestClient

        monkeypatch.setenv("GATEWAY_PROVIDER_MODE", "real")
        monkeypatch.delenv("GATEWAY_DB_DSN", raising=False)
        monkeypatch.delenv("GATEWAY_CONFIG", raising=False)

        # Re-import the app module so the lifespan picks up the new env.
        import sys
        if "gateway.app" in sys.modules:
            del sys.modules["gateway.app"]
        from gateway.app import app as real_app

        with pytest.raises(RuntimeError, match="GATEWAY_DB_DSN"):
            with TestClient(real_app):
                pass  # lifespan startup should raise before we get here
