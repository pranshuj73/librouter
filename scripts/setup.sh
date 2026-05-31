#!/bin/sh
# One-time environment bootstrap: apply migrations and seed callers.
# Run this once when bootstrapping a new environment:
#   ./scripts/setup.sh
#
# Required env vars:
#   GATEWAY_DB_DSN          — postgres DSN (defaults to local dev if unset)
#   GATEWAY_SEED_KEY_<NAME> — plaintext key per caller in caller-seeding.json
set -euo pipefail

echo "applying migrations..."
python -m scripts.apply_migrations

echo "seeding callers..."
python -m scripts.seed_callers

echo "setup complete"
