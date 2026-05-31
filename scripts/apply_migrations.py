"""Apply all pending SQL migrations to the gateway database.

Usage (env-driven, no arg parsing):
    GATEWAY_DB_DSN=postgres://... python scripts/apply_migrations.py
    python -m scripts.apply_migrations

Exits 0 on success, non-zero on failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_DSN = "postgres://gateway:gateway@localhost:5432/gateway"


async def main() -> None:
    dsn = os.environ.get("GATEWAY_DB_DSN", _DEFAULT_DSN)
    if dsn == _DEFAULT_DSN:
        log.warning("GATEWAY_DB_DSN not set — using default local DSN")

    # Import here so the module is importable even without gateway on sys.path
    # when run via `python scripts/apply_migrations.py` from the repo root.
    from gateway.db import Database

    db = Database(dsn=dsn)
    try:
        await db.connect()
    except Exception as exc:
        log.error("failed to connect to database: %s", exc)
        sys.exit(1)

    try:
        await db.run_migrations()
        log.info("migrations complete")
    except Exception as exc:
        log.error("migration failed: %s", exc)
        sys.exit(1)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
