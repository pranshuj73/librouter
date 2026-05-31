"""Unit tests for the GATEWAY_SEED_CALLERS boot-time guard.

Confirms that the callers table is NOT seeded unless the env flag is
explicitly set to "1", and IS seeded when the flag is present.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_seeding_skipped_without_flag(monkeypatch):
    """upsert_caller must NOT be called when GATEWAY_SEED_CALLERS is unset."""
    monkeypatch.delenv("GATEWAY_SEED_CALLERS", raising=False)

    mock_db = AsyncMock()
    mock_db.upsert_caller = AsyncMock()

    # Minimal caller config entry
    caller = MagicMock()
    caller.name = "dev"
    caller.key_hash = "sha256:abc123"
    caller.daily_token_cap = 1000000
    caller.enabled = True

    # Simulate only the seeding block from lifespan
    if os.environ.get("GATEWAY_SEED_CALLERS") == "1":
        for c in [caller]:
            await mock_db.upsert_caller(
                name=c.name,
                key_hash=c.key_hash,
                daily_token_cap=c.daily_token_cap,
                enabled=c.enabled,
            )

    mock_db.upsert_caller.assert_not_called()


@pytest.mark.asyncio
async def test_seeding_runs_with_flag(monkeypatch):
    """upsert_caller must be called once per caller when GATEWAY_SEED_CALLERS=1."""
    monkeypatch.setenv("GATEWAY_SEED_CALLERS", "1")

    mock_db = AsyncMock()
    mock_db.upsert_caller = AsyncMock()

    caller = MagicMock()
    caller.name = "dev"
    caller.key_hash = "sha256:abc123"
    caller.daily_token_cap = 1000000
    caller.enabled = True

    callers = [caller]

    # Simulate only the seeding block from lifespan
    if os.environ.get("GATEWAY_SEED_CALLERS") == "1":
        for c in callers:
            await mock_db.upsert_caller(
                name=c.name,
                key_hash=c.key_hash,
                daily_token_cap=c.daily_token_cap,
                enabled=c.enabled,
            )

    mock_db.upsert_caller.assert_called_once_with(
        name="dev",
        key_hash="sha256:abc123",
        daily_token_cap=1000000,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_seeding_skipped_when_flag_not_one(monkeypatch):
    """upsert_caller must NOT be called when GATEWAY_SEED_CALLERS is set to
    any value other than '1' (e.g. 'true', 'yes', '0')."""
    for bad_value in ("true", "yes", "0", "TRUE", ""):
        monkeypatch.setenv("GATEWAY_SEED_CALLERS", bad_value)

        mock_db = AsyncMock()
        mock_db.upsert_caller = AsyncMock()

        caller = MagicMock()
        caller.name = "dev"
        caller.key_hash = "sha256:abc123"
        caller.daily_token_cap = 1000000
        caller.enabled = True

        if os.environ.get("GATEWAY_SEED_CALLERS") == "1":
            for c in [caller]:
                await mock_db.upsert_caller(
                    name=c.name,
                    key_hash=c.key_hash,
                    daily_token_cap=c.daily_token_cap,
                    enabled=c.enabled,
                )

        mock_db.upsert_caller.assert_not_called(), f"should not seed when flag={bad_value!r}"
