#!/usr/bin/env bash
# Live-vendor smoke test for the gateway.
#
# Prereqs:
#   1. cp .env.example .env  (then fill in OPENAI_API_KEY and/or GOOGLE_API_KEY)
#   2. ./scripts/setup.sh must have been run first (applies migrations and seeds callers)
#   3. docker compose up -d --build
#   4. wait ~3s for the gateway to come up
#
# Then:
#   ./scripts/real_provider_smoke.sh
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
CALLER_KEY="${CALLER_KEY:-dev-key-do-not-use-in-prod}"

echo "== /healthz =="
curl -sf "${GATEWAY_URL}/healthz" && echo

echo "== /readyz =="
curl -sf "${GATEWAY_URL}/readyz" && echo

echo "== /v1/chat/completions on 'fast' tier =="
curl -sf -X POST "${GATEWAY_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${CALLER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fast",
    "messages": [{"role": "user", "content": "Reply with exactly one word: hello"}],
    "max_tokens": 16,
    "temperature": 0.0
  }' | tee /dev/stderr | grep -q '"role":"assistant"' || {
    echo "FAILED: response did not contain an assistant message" >&2
    exit 1
}
echo

echo "== /v1/chat/completions on 'smart' tier =="
curl -sf -X POST "${GATEWAY_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${CALLER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart",
    "messages": [{"role": "user", "content": "Reply with exactly one word: world"}],
    "max_tokens": 16,
    "temperature": 0.0
  }' | tee /dev/stderr | grep -q '"role":"assistant"' || {
    echo "FAILED: response did not contain an assistant message" >&2
    exit 1
}
echo

echo "== which providers actually served =="
curl -sf "${GATEWAY_URL}/metrics" \
  | grep -E '^gateway_attempts_total{.*status="ok"' \
  || echo "(no ok attempts recorded yet)"

echo
echo "== effective routing weights =="
curl -sf "${GATEWAY_URL}/metrics" \
  | grep -E '^gateway_routing_weight'

echo
echo "Smoke OK."
