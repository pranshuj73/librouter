"""Shared pytest fixtures.

We use `fakeredis` (with the `lua` extra) for unit-level Redis tests so the
suite runs without Docker and finishes in milliseconds. The e2e test in step
14 still uses a real testcontainers Redis to catch any divergence.

This module also provides shared builders (config / caller) so individual
test modules do not each re-roll a slightly-different `_config()` factory —
which is a known source of "test passes for the wrong reason" bugs flagged
in `docs/code-review/t-1.md` §1 and §20.
"""

from __future__ import annotations

import copy
import os
import shutil
from typing import Any

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio

from gateway.models import Caller, Config


# ---------------------------------------------------------------- Redis (unchanged)


@pytest_asyncio.fixture
async def redis():
    """A clean fakeredis async client per test."""
    r = fakeredis_aio.FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.flushall()
        await r.aclose()


# ---------------------------------------------------------------- Config builder
#
# Per §1 / §20 of the test review: hoist the duplicated `_config()` builders
# out of `test_router.py`, `test_refresh.py`, `test_app_e2e.py`, and
# `test_build_vendors.py`. The canonical shape is the 3-vendor "fast" tier
# (openai/anthropic/google) with full price + rate_limit coverage.

_DEFAULT_TIERS: dict[str, list[dict[str, Any]]] = {
    "fast": [
        {"provider": "openai", "model": "gpt-4o-mini", "weight": 33.0},
        {"provider": "anthropic", "model": "haiku", "weight": 33.0},
        {"provider": "google", "model": "gemini-flash", "weight": 33.0},
    ],
}

_DEFAULT_ROUTING: dict[str, Any] = {
    "refresh_interval_ms": 100,
    "health_window_s": 60,
    "target_latency_s": 3.0,
    "min_weight_floor": 0.001,
}

_DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "anthropic/haiku": {"input": 1.0, "output": 5.0},
    "google/gemini-flash": {"input": 0.3, "output": 2.5},
}

_DEFAULT_RATE_LIMITS: dict[str, dict[str, int]] = {
    "openai/gpt-4o-mini": {"rpm": 1000, "tpm": 100_000},
    "anthropic/haiku": {"rpm": 1000, "tpm": 100_000},
    "google/gemini-flash": {"rpm": 1000, "tpm": 100_000},
}

_DEFAULT_CALLERS: list[dict[str, Any]] = [
    {"name": "test", "key_hash": "sha256:abc", "daily_token_cap": 1_000_000},
]


def make_config(
    *,
    provider_mode: str = "mock",
    secrets_mode: str = "mock",
    tiers: dict[str, list[dict[str, Any]]] | None = None,
    routing: dict[str, Any] | None = None,
    prices: dict[str, dict[str, float]] | None = None,
    rate_limits: dict[str, dict[str, int]] | None = None,
    callers: list[dict[str, Any]] | None = None,
) -> Config:
    """Build a valid `Config` matching the 3-vendor "fast" tier shape.

    Any field is overridable; the defaults match the shape used in
    `test_router.py`, `test_refresh.py`, `test_build_vendors.py`, and
    `test_app_e2e.py`.
    """
    payload: dict[str, Any] = {
        "provider_mode": provider_mode,
        "secrets_mode": secrets_mode,
        "tiers": copy.deepcopy(tiers if tiers is not None else _DEFAULT_TIERS),
        "routing": copy.deepcopy(routing if routing is not None else _DEFAULT_ROUTING),
        "prices": copy.deepcopy(prices if prices is not None else _DEFAULT_PRICES),
        "rate_limits": copy.deepcopy(
            rate_limits if rate_limits is not None else _DEFAULT_RATE_LIMITS
        ),
        "callers": copy.deepcopy(callers if callers is not None else _DEFAULT_CALLERS),
    }
    return Config.model_validate(payload)


def simple_caller(name: str = "test") -> Caller:
    """Build a plain enabled `Caller` with a generous daily cap."""
    return Caller(name=name, daily_token_cap=1_000_000, enabled=True)


# ---------------------------------------------------------------- Docker gate
#
# `test_db.py` and `test_app_e2e.py` both skip at module-level when Docker
# isn't available. They can wire this marker in later (§5.1 of the review);
# we expose it now so the gate is defined in one place.

requires_docker = pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("SKIP_DOCKER_TESTS") == "1",
    reason="Docker not available (or SKIP_DOCKER_TESTS=1)",
)


# ---------------------------------------------------------------- Prometheus reset
#
# Per §1 / §20.2 of the test review: `gateway/metrics.py` registers all
# Counter / Gauge / Histogram collectors at module-import time against a
# module-scoped `CollectorRegistry`. Without explicit reset between tests,
# counters in test N see the cumulative value from tests 0..N-1, and tests
# that scrape `/metrics` end up reading state they didn't produce.
#
# Each labelled collector exposes `.clear()` which drops every labelled
# child (e.g. all `(caller, tier, outcome)` triples seen so far). That's
# exactly the "start at zero" semantics we want between tests.


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Function-scoped autouse: clear all gateway prometheus collectors.

    Runs before AND after each test (yield in the middle). Iterates the
    collectors registered on `gateway.metrics.REGISTRY` and calls
    `.clear()` on any collector that supports it. This covers labelled
    Counters / Gauges / Histograms (the ones we actually use in
    `gateway/metrics.py`).
    """
    try:
        from gateway.metrics import REGISTRY  # noqa: WPS433 — local import is intentional
    except Exception:  # pragma: no cover — defensive; the import must work
        yield
        return

    def _clear_all() -> None:
        # `_collector_to_names` is the documented private attribute used by
        # the prometheus_client tests themselves. It maps Collector -> {name}.
        for collector in list(REGISTRY._collector_to_names.keys()):
            clear = getattr(collector, "clear", None)
            if callable(clear):
                try:
                    clear()
                except Exception:
                    # Some collectors (unlabelled) raise; ignore — they
                    # have no labelled-child state to drop.
                    pass

    _clear_all()
    try:
        yield
    finally:
        _clear_all()


# ---------------------------------------------------------------- Optional clock helper


@pytest.fixture
def freeze_clock() -> list[float]:
    """Mutable single-element clock list usable with `now_s_fn=lambda: clock[0]`.

    Tests can advance time by mutating `clock[0]`. Harmless if unused.
    """
    return [0.0]
