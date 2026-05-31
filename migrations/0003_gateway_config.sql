-- Migration 0003: Move gateway runtime configuration into Postgres.
-- Idempotent (CREATE TABLE IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS tiers (
  name           TEXT PRIMARY KEY,
  fallback_tier  TEXT  -- FK + cycle-check will be added by the sibling agent
);

CREATE TABLE IF NOT EXISTS tier_models (
  provider  TEXT PRIMARY KEY,
  config    JSONB NOT NULL
  -- config JSON shape:
  -- {
  --   "<tier_name>": {
  --     "model": "<vendor-model-id>",
  --     "weight": <float>,
  --     "rate_limits": {"rpm": <int>, "tpm": <int>}
  --   },
  --   "<another_tier>": { ... }
  -- }
);

CREATE TABLE IF NOT EXISTS routing_config (
  id                  INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  refresh_interval_ms INT NOT NULL DEFAULT 1000,
  health_window_s     INT NOT NULL DEFAULT 60,
  target_latency_s    NUMERIC NOT NULL DEFAULT 3.0,
  min_weight_floor    NUMERIC NOT NULL DEFAULT 0.02,
  rng_seed_env        TEXT
);
