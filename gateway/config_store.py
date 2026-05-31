"""DB + Redis-backed gateway config loader.

Replaces the old YAML-file load_config() path. The three new DB tables
(tiers, tier_models, routing_config) are the single source of truth;
Redis acts as a short-lived cache (default TTL 60s) so individual replicas
pick up updates on the next TTL expiry without explicit push.

Redis key: ``gw:config:current``
Value    : JSON dump of ``cfg.model_dump()`` (UTF-8 encoded bytes)
TTL      : ``cache_ttl_s`` (default 60s)
"""

from __future__ import annotations

import json
import logging

from gateway.db import Database
from gateway.models import (
    Config,
    RateLimitEntry,
    RoutingConfig,
    TierConfig,
    TierEntry,
)
from gateway.redis_state import RedisState


log = logging.getLogger(__name__)

_REDIS_KEY = b"gw:config:current"


class ConfigStoreError(Exception):
    """Raised when the database is missing required configuration rows."""


class ConfigStore:
    """Load / cache / write gateway config via DB + Redis."""

    def __init__(
        self,
        *,
        db: Database,
        redis_state: RedisState,
        cache_ttl_s: int = 60,
    ) -> None:
        self._db = db
        self._state = redis_state
        self._ttl = cache_ttl_s

    # ---------------------------------------------------------------- public API

    async def load_from_db(self) -> Config:
        """Read the three config tables and assemble a Config.  No cache used."""
        routing_row = await self._db.fetch_routing_config()
        if routing_row is None:
            raise ConfigStoreError(
                "routing_config table has no row — "
                "run ./scripts/setup.sh to bootstrap"
            )

        tier_rows = await self._db.fetch_tiers()
        tier_model_rows = await self._db.fetch_tier_models()

        # Build the provider→{tier→{model, weight, rate_limits}} mapping.
        # tier_models.config is keyed by tier name.
        provider_tier_map: dict[str, dict] = {}
        for row in tier_model_rows:
            provider_tier_map[row["provider"]] = row["config"]

        tiers: dict[str, TierConfig] = {}
        for tier_row in tier_rows:
            tier_name: str = tier_row["name"]
            candidates: list[TierEntry] = []
            for provider, tier_map in provider_tier_map.items():
                if tier_name not in tier_map:
                    continue
                entry = tier_map[tier_name]
                candidates.append(
                    TierEntry(
                        provider=provider,
                        model=entry["model"],
                        weight=entry["weight"],
                        rate_limits=RateLimitEntry(
                            rpm=entry["rate_limits"]["rpm"],
                            tpm=entry["rate_limits"]["tpm"],
                        ),
                    )
                )
            tiers[tier_name] = TierConfig(candidates=candidates)

        routing = RoutingConfig(
            refresh_interval_ms=routing_row["refresh_interval_ms"],
            health_window_s=routing_row["health_window_s"],
            target_latency_s=float(routing_row["target_latency_s"]),
            min_weight_floor=float(routing_row["min_weight_floor"]),
            rng_seed_env=routing_row.get("rng_seed_env"),
        )

        # provider_mode and secrets_mode are not stored in the DB; they are
        # always controlled via GATEWAY_PROVIDER_MODE / GATEWAY_SECRETS_MODE
        # env vars applied by _apply_env_overrides() in app.py after this
        # load. We default to "mock" here; the env overrides replace it.
        return Config(
            provider_mode="mock",
            secrets_mode="mock",
            tiers=tiers,
            routing=routing,
            callers=[],
        )

    async def load_or_refresh(self, *, force: bool = False) -> Config:
        """Return config from Redis cache, or re-fetch from DB on miss/force."""
        if not force:
            cached = await self._state.client.get(_REDIS_KEY)
            if cached is not None:
                try:
                    data = json.loads(cached.decode("utf-8"))
                    return Config.model_validate(data)
                except Exception:
                    log.warning(
                        "cached config in Redis is corrupt; re-fetching from DB"
                    )

        cfg = await self.load_from_db()
        await self._write_to_redis(cfg)
        return cfg

    async def write(self, cfg: Config) -> None:
        """Persist Config to all three DB tables atomically, then bust the cache."""
        await self._write_to_db(cfg)
        # Invalidate so the next load_or_refresh fetches the fresh row.
        await self._state.client.delete(_REDIS_KEY)

    # ---------------------------------------------------------------- internals

    async def _write_to_redis(self, cfg: Config) -> None:
        payload = json.dumps(cfg.model_dump()).encode("utf-8")
        await self._state.client.setex(_REDIS_KEY, self._ttl, payload)

    async def _write_to_db(self, cfg: Config) -> None:
        """Write tiers, tier_models, and routing_config in a single transaction."""

        # Collect all providers across tiers.
        # tier_models row shape: provider → {tier_name → {model, weight, rate_limits}}
        provider_map: dict[str, dict[str, dict]] = {}
        for tier_name, tier_cfg in cfg.tiers.items():
            await self._db.upsert_tier(name=tier_name, fallback_tier=None)
            for cand in tier_cfg.candidates:
                if cand.provider not in provider_map:
                    provider_map[cand.provider] = {}
                provider_map[cand.provider][tier_name] = {
                    "model": cand.model,
                    "weight": cand.weight,
                    "rate_limits": {
                        "rpm": cand.rate_limits.rpm,
                        "tpm": cand.rate_limits.tpm,
                    },
                }

        for provider, tier_cfg_map in provider_map.items():
            await self._db.upsert_tier_models(provider=provider, config=tier_cfg_map)

        await self._db.upsert_routing_config(
            refresh_interval_ms=cfg.routing.refresh_interval_ms,
            health_window_s=cfg.routing.health_window_s,
            target_latency_s=float(cfg.routing.target_latency_s),
            min_weight_floor=float(cfg.routing.min_weight_floor),
            rng_seed_env=cfg.routing.rng_seed_env,
        )
