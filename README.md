# LLM Gateway

Internal LLM gateway with weighted autorouting across OpenAI / Anthropic / Google.

See [`docs/PLAN.md`](docs/PLAN.md) for the full design and [`docs/PROGRESS.md`](docs/PROGRESS.md) for implementation status.

## Quick start (mock vendors, no credentials)

```bash
docker compose up -d --build
curl -sX POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer dev-key-do-not-use-in-prod" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

The default compose runs **mock vendors** so no API keys are needed.

## Using real providers

Bring up the stack against actual OpenAI / Anthropic / Google APIs.

1. Copy the env template and fill in real keys for whichever providers you have:

   ```bash
   cp .env.example .env
   # edit .env — uncomment & set OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY
   ```

   `.env` is gitignored. Only set the keys you have; vendors with missing keys are skipped at boot with a warning, and tier candidates pointing at them get effective weight `0` (so the router never picks them).

2. Build & start:

   ```bash
   docker compose up -d --build
   ```

3. Smoke test:

   ```bash
   ./scripts/real_provider_smoke.sh
   ```

   The script hits `/healthz`, `/readyz`, and both tiers (`fast` and `smart`), then dumps `gateway_attempts_total` and `gateway_routing_weight` so you can see which vendor actually served each request.

### Tier configuration

By default `.env` points `GATEWAY_CONFIG` at `config.dev.yaml`, which has the dev caller `"dev-key-do-not-use-in-prod"` pre-baked. Tier candidates default to:

- `fast` — Anthropic Haiku, OpenAI 4o-mini, Gemini 2.5 Flash (50/30/20)
- `smart` — Anthropic Sonnet, OpenAI 4o, Gemini 2.5 Pro (40/40/20)

If a model name is wrong for your access tier (Google in particular evolves rapidly), edit `config.dev.yaml` and restart the gateway — configs are bind-mounted so no rebuild is needed:

```bash
docker compose restart gateway
```

To remove a vendor entirely (say, you only want OpenAI), edit `config.dev.yaml` and delete the relevant entries from the `tiers:` / `prices:` / `rate_limits:` blocks. Or just leave them — they'll get weight 0 automatically.

### Switching back to mocks

Either delete `.env` or comment out `GATEWAY_PROVIDER_MODE` / `GATEWAY_SECRETS_MODE` in it. The compose substitutions default both to `mock`.

## Tests

```bash
uv pip install -e '.[dev]'
.venv/bin/pytest                                    # all 148 tests (~3 min, needs Docker for testcontainers Postgres/Redis)
.venv/bin/pytest --ignore=tests/test_db.py --ignore=tests/test_app_e2e.py    # 140 fast tests, no Docker
```
