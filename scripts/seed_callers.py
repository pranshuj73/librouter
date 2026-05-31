"""Seed the callers table from scripts/data/caller-seeding.json.

For each caller entry, looks up GATEWAY_SEED_KEY_<NAME_UPPER> in the
environment (dashes become underscores, all upper-case). If set, hashes
the plaintext with the HMAC-SHA256 pepper and upserts the caller row.
If absent, prints a warning and continues.

GATEWAY_KEY_HASH_PEPPER must be set before running this script — if it is
empty the script aborts immediately with a non-zero exit code so that CI
catches a misconfigured run early.

Usage (env-driven, no arg parsing):
    GATEWAY_KEY_HASH_PEPPER=<pepper> GATEWAY_SEED_KEY_DEV=dev-key python scripts/seed_callers.py
    python -m scripts.seed_callers

Exits 0 in all cases (skipping missing keys is normal in CI), except:
- Exit 1 if GATEWAY_KEY_HASH_PEPPER is not set (empty).
- Exit 1 if the DB connection fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_DSN = "postgres://gateway:gateway@localhost:5432/gateway"
_SEEDING_JSON = Path(__file__).resolve().parent / "data" / "caller-seeding.json"


def _env_key_name(caller_name: str) -> str:
    """Map a caller name to its env var name, e.g. 'search-svc' -> 'GATEWAY_SEED_KEY_SEARCH_SVC'."""
    normalized = caller_name.replace("-", "_").upper()
    return f"GATEWAY_SEED_KEY_{normalized}"


async def seed_from_json(db: Any, json_path: Path, env: dict[str, str], *, pepper: str) -> None:
    """Core seeding loop — importable for testing with a stub db.

    `pepper` must be a non-empty string; the function delegates validation to
    `hash_api_key` which raises ValueError if it is falsy.
    """
    callers = json.loads(json_path.read_text())
    for entry in callers:
        name: str = entry["name"]
        env_var = _env_key_name(name)
        plaintext = env.get(env_var)
        if plaintext is None:
            print(f"WARN: skipping {name}: {env_var} not set")
            continue
        from gateway.auth import hash_api_key
        key_hash = hash_api_key(plaintext, pepper=pepper)
        await db.upsert_caller(
            name=name,
            key_hash=key_hash,
            daily_token_cap=entry.get("daily_token_cap"),
            enabled=entry.get("enabled", True),
        )
        print(f"seeded caller={name}")


async def main() -> None:
    pepper = os.environ.get("GATEWAY_KEY_HASH_PEPPER", "").strip()
    if not pepper:
        print(
            "ERROR: GATEWAY_KEY_HASH_PEPPER is not set or empty. "
            "Generate one with: openssl rand -hex 32",
            file=sys.stderr,
        )
        sys.exit(1)

    dsn = os.environ.get("GATEWAY_DB_DSN", _DEFAULT_DSN)
    if dsn == _DEFAULT_DSN:
        log.warning("GATEWAY_DB_DSN not set — using default local DSN")

    from gateway.db import Database

    db = Database(dsn=dsn)
    try:
        await db.connect()
    except Exception as exc:
        log.error("failed to connect to database: %s", exc)
        sys.exit(1)

    try:
        await seed_from_json(db, _SEEDING_JSON, dict(os.environ), pepper=pepper)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
