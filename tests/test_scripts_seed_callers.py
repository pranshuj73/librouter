"""Tests for the scripts/seed_callers.py seeding logic.

Uses a stub Database (no real Postgres) and a temporary JSON file so the
suite runs fast without testcontainers.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

from scripts.seed_callers import seed_from_json


class StubDatabase:
    """Minimal stub — records upsert_caller calls."""

    def __init__(self):
        self.upsert_caller = AsyncMock()


@pytest.fixture()
def json_path(tmp_path: Path) -> Path:
    p = tmp_path / "callers.json"
    p.write_text(json.dumps([
        {"name": "dev", "daily_token_cap": 1000000, "enabled": True},
        {"name": "search-svc", "daily_token_cap": 500000, "enabled": True},
    ]))
    return p


@pytest.fixture()
def empty_json_path(tmp_path: Path) -> Path:
    p = tmp_path / "empty.json"
    p.write_text("[]")
    return p


@pytest.mark.asyncio
async def test_all_env_vars_present_upserts_all(json_path: Path):
    """When env vars are set for all callers, all are upserted."""
    db = StubDatabase()
    env = {
        "GATEWAY_SEED_KEY_DEV": "plaintext-dev",
        "GATEWAY_SEED_KEY_SEARCH_SVC": "plaintext-search",
    }
    await seed_from_json(db, json_path, env)

    assert db.upsert_caller.call_count == 2
    names_called = {c.kwargs["name"] for c in db.upsert_caller.call_args_list}
    assert names_called == {"dev", "search-svc"}


@pytest.mark.asyncio
async def test_missing_env_var_skips_caller(json_path: Path):
    """When one env var is absent, that caller is skipped; others are upserted."""
    db = StubDatabase()
    env = {
        "GATEWAY_SEED_KEY_DEV": "plaintext-dev",
        # GATEWAY_SEED_KEY_SEARCH_SVC intentionally absent
    }
    await seed_from_json(db, json_path, env)

    assert db.upsert_caller.call_count == 1
    assert db.upsert_caller.call_args.kwargs["name"] == "dev"


@pytest.mark.asyncio
async def test_empty_json_no_upserts(empty_json_path: Path):
    """Empty JSON file results in no upserts and no errors."""
    db = StubDatabase()
    await seed_from_json(db, empty_json_path, {})

    db.upsert_caller.assert_not_called()


@pytest.mark.asyncio
async def test_key_is_hashed_before_upsert(json_path: Path):
    """The plaintext key must be hashed (sha256: prefix) before upsert."""
    from gateway.auth import hash_api_key

    db = StubDatabase()
    env = {
        "GATEWAY_SEED_KEY_DEV": "my-secret",
        "GATEWAY_SEED_KEY_SEARCH_SVC": "other-secret",
    }
    await seed_from_json(db, json_path, env)

    dev_call = next(
        c for c in db.upsert_caller.call_args_list if c.kwargs["name"] == "dev"
    )
    assert dev_call.kwargs["key_hash"] == hash_api_key("my-secret")
    assert dev_call.kwargs["key_hash"].startswith("sha256:")
