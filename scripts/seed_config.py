"""Seed gateway configuration from a YAML file into Postgres.

Usage:
    python -m scripts.seed_config [yaml_path]

    yaml_path defaults to scripts/data/config-seeding.yaml.

Required env var:
    GATEWAY_DB_DSN — asyncpg-compatible Postgres DSN.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import yaml

# Allow running as a script without installing the package.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gateway.config_store import ConfigStore
from gateway.db import Database
from gateway.models import Config
from gateway.redis_state import RedisState


_DEFAULT_YAML = Path(__file__).resolve().parent / "data" / "config-seeding.yaml"


async def seed_from_yaml(
    yaml_path: str | Path, config_store: ConfigStore
) -> None:
    """Parse the YAML at *yaml_path* and write it to the DB via *config_store*.

    This is the testable core — the __main__ block just wires the DB and calls
    this function.
    """
    raw = Path(yaml_path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    cfg = Config.model_validate(data)
    await config_store.write(cfg)


async def _main(yaml_path: Path) -> None:
    dsn = os.environ.get("GATEWAY_DB_DSN", "")
    if not dsn:
        dsn = "postgres://gateway:gateway@localhost:5432/gateway"
        print(
            "GATEWAY_DB_DSN not set; using default dev DSN. "
            "Set GATEWAY_DB_DSN for non-dev environments.",
            file=sys.stderr,
        )

    db = Database(dsn=dsn)
    await db.connect()
    try:
        await db.run_migrations()

        import redis.asyncio as redis_async
        redis_url = os.environ.get("GATEWAY_REDIS_URL", "redis://localhost:6379/0")
        r = redis_async.from_url(redis_url, decode_responses=False)
        state = RedisState(r)
        await state.load_scripts()

        store = ConfigStore(db=db, redis_state=state)
        await seed_from_yaml(yaml_path, store)

        await r.aclose()
        print(f"Config seeded from {yaml_path}")
    finally:
        await db.close()


if __name__ == "__main__":
    _yaml = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_YAML
    if not _yaml.exists():
        print(f"ERROR: YAML file not found: {_yaml}", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_main(_yaml))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
