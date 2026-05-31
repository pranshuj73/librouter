#!/bin/sh
# One-time environment bootstrap: apply migrations, seed config, seed callers.
# Run this once when bootstrapping a new environment:
#   ./scripts/setup.sh
#
# Required env vars:
#   GATEWAY_DB_DSN          — postgres DSN (defaults to local dev if unset)
#   GATEWAY_REDIS_URL       — Redis URL (defaults to local dev if unset)
#   GATEWAY_SEED_KEY_<NAME> — plaintext key per caller in caller-seeding.json
set -euo pipefail

echo "applying migrations..."
python -m scripts.apply_migrations

echo "seeding gateway config..."
python -m scripts.seed_config

echo "seeding callers..."
python -m scripts.seed_callers

echo "setup complete"
