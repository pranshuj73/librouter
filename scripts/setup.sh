#!/bin/sh
# One-time environment bootstrap: apply migrations, seed config, seed callers.
# Run this once when bootstrapping a new environment:
#   ./scripts/setup.sh
#
# Required env vars (loaded from ./.env if present):
#   GATEWAY_DB_DSN            — postgres DSN (defaults to local dev if unset)
#   GATEWAY_REDIS_URL         — Redis URL (defaults to local dev if unset)
#   GATEWAY_KEY_HASH_PEPPER   — required by seed_callers
#   GATEWAY_SEED_KEY_<NAME>   — plaintext key per caller in caller-seeding.json
set -eu

# Source ./.env into this shell so the Python scripts inherit the vars.
# docker-compose loads .env automatically; this script does not, hence the
# explicit sourcing here.
if [ -f ./.env ]; then
  echo "loading ./.env"
  set -a
  . ./.env
  set +a
fi

echo "applying migrations..."
python -m scripts.apply_migrations

echo "seeding gateway config..."
python -m scripts.seed_config

echo "seeding callers..."
python -m scripts.seed_callers

echo "setup complete"
