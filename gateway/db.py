"""asyncpg pool + batched-writer + caller queries.

`Database.run_migrations()` reads the .sql files in `migrations/` and applies
them inside a transaction. Migrations are written to be idempotent
(`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`) so re-running is
safe and tests can call it freely.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from gateway.models import AttemptRecord, Caller


log = logging.getLogger(__name__)


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class Database:
    def __init__(self, *, dsn: str, min_size: int = 1, max_size: int = 5) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn, min_size=self._min, max_size=self._max
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() not called")
        return self._pool

    # ---------------------------------------------------------------- migrations

    async def run_migrations(self) -> None:
        # Fail fast before touching the pool so we don't hold a connection
        # slot while raising a configuration error.
        if not _MIGRATIONS_DIR.exists():
            raise RuntimeError(
                f"migrations directory not found: {_MIGRATIONS_DIR}. "
                "Ensure the 'migrations/' directory is present in the Docker image."
            )
        files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            raise RuntimeError(
                f"migrations directory is empty (no *.sql files): {_MIGRATIONS_DIR}. "
                "At least one migration file is required."
            )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize concurrent migration runs across replicas with a
                # Postgres transactional advisory lock.  The lock is released
                # automatically when the transaction commits or rolls back.
                # Lock ID 7331101 is an arbitrary project-specific constant
                # chosen to avoid collisions with other advisory locks in the
                # same cluster.
                await conn.execute("SELECT pg_advisory_xact_lock($1)", 7331101)
                for f in files:
                    sql = f.read_text()
                    await conn.execute(sql)
                    log.info("applied migration %s", f.name)

    # ---------------------------------------------------------------- writes

    async def write_batch(self, records: list[AttemptRecord]) -> None:
        if not records:
            return
        rows = [
            (
                r.request_id,
                r.caller,
                r.tier,
                r.provider,
                r.model,
                r.attempt_idx,
                r.input_tokens,
                r.output_tokens,
                r.cost_usd,
                r.latency_ms,
                r.status,
                r.vendor_req_id,
                r.client_trace_id,
            )
            for r in records
        ]
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO requests
                    (request_id, caller, tier, provider, model,
                     attempt_idx, input_tokens, output_tokens, cost_usd,
                     latency_ms, status, vendor_req_id, client_trace_id)
                VALUES
                    ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                rows,
            )

    async def upsert_caller(
        self, *, name: str, key_hash: str, daily_token_cap: int | None, enabled: bool = True
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO callers (name, key_hash, daily_token_cap, enabled)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (name) DO UPDATE
                  SET key_hash        = EXCLUDED.key_hash,
                      daily_token_cap = EXCLUDED.daily_token_cap,
                      enabled         = EXCLUDED.enabled
                """,
                name,
                key_hash,
                daily_token_cap,
                enabled,
            )

    # ---------------------------------------------------------------- reads

    async def caller_by_key_hash(self, key_hash: str) -> Caller | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, daily_token_cap, enabled FROM callers WHERE key_hash = $1",
                key_hash,
            )
        if row is None:
            return None
        return Caller(name=row[0], daily_token_cap=row[1], enabled=row[2])

    async def caller_tokens_used_today(self, caller: str) -> int:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self.pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT COALESCE(SUM(input_tokens + output_tokens), 0)::BIGINT
                FROM requests
                WHERE caller = $1 AND ts >= $2
                """,
                caller,
                today_start,
            )
        return int(val or 0)

    # ---------------------------------------------------------------- config tables (new in 0003)

    async def fetch_tiers(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, fallback_tier FROM tiers ORDER BY name")
        return [dict(r) for r in rows]

    async def upsert_tier(self, *, name: str, fallback_tier: str | None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tiers (name, fallback_tier)
                VALUES ($1, $2)
                ON CONFLICT (name) DO UPDATE
                  SET fallback_tier = EXCLUDED.fallback_tier
                """,
                name,
                fallback_tier,
            )

    async def fetch_tier_models(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT provider, config FROM tier_models ORDER BY provider")
        return [{"provider": r["provider"], "config": json.loads(r["config"])} for r in rows]

    async def upsert_tier_models(self, *, provider: str, config: dict) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tier_models (provider, config)
                VALUES ($1, $2::jsonb)
                ON CONFLICT (provider) DO UPDATE
                  SET config = EXCLUDED.config
                """,
                provider,
                json.dumps(config),
            )

    async def fetch_routing_config(self) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT refresh_interval_ms, health_window_s, target_latency_s,
                       min_weight_floor, rng_seed_env
                FROM routing_config
                WHERE id = 1
                """
            )
        if row is None:
            return None
        return dict(row)

    async def upsert_routing_config(
        self,
        *,
        refresh_interval_ms: int,
        health_window_s: int,
        target_latency_s: float,
        min_weight_floor: float,
        rng_seed_env: str | None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO routing_config
                    (id, refresh_interval_ms, health_window_s,
                     target_latency_s, min_weight_floor, rng_seed_env)
                VALUES (1, $1, $2, $3, $4, $5)
                ON CONFLICT (id) DO UPDATE
                  SET refresh_interval_ms = EXCLUDED.refresh_interval_ms,
                      health_window_s     = EXCLUDED.health_window_s,
                      target_latency_s    = EXCLUDED.target_latency_s,
                      min_weight_floor    = EXCLUDED.min_weight_floor,
                      rng_seed_env        = EXCLUDED.rng_seed_env
                """,
                refresh_interval_ms,
                health_window_s,
                target_latency_s,
                min_weight_floor,
                rng_seed_env,
            )

    async def usage_summary(
        self, *, caller: str | None = None, since: datetime | None = None
    ) -> list[dict]:
        clauses: list[str] = []
        args: list = []
        if caller is not None:
            clauses.append(f"caller = ${len(args) + 1}")
            args.append(caller)
        if since is not None:
            clauses.append(f"ts >= ${len(args) + 1}")
            args.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
        SELECT caller,
               provider,
               model,
               COUNT(*)                          AS attempts,
               SUM(input_tokens + output_tokens) AS tokens,
               SUM(cost_usd)::FLOAT              AS cost_usd
        FROM requests
        {where}
        GROUP BY caller, provider, model
        ORDER BY caller, provider, model
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]
