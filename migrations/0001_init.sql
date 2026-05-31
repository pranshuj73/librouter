-- Idempotent init migration for the gateway DB.
-- Mounted by docker-compose into /docker-entrypoint-initdb.d so the schema
-- is created on first boot, and re-applied safely by `db.py` on startup.

CREATE TABLE IF NOT EXISTS requests (
  id            BIGSERIAL PRIMARY KEY,
  request_id    TEXT NOT NULL,
  caller        TEXT NOT NULL,
  tier          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  attempt_idx   SMALLINT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
  latency_ms    INTEGER NOT NULL,
  status        TEXT NOT NULL,
  vendor_req_id TEXT,
  ts            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS requests_caller_ts_idx ON requests (caller, ts DESC);
CREATE INDEX IF NOT EXISTS requests_ts_idx        ON requests (ts DESC);

CREATE TABLE IF NOT EXISTS callers (
  name             TEXT PRIMARY KEY,
  key_hash         TEXT NOT NULL,
  daily_token_cap  BIGINT,
  enabled          BOOLEAN NOT NULL DEFAULT TRUE
);
