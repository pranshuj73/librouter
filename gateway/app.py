"""FastAPI gateway entrypoint.

Wires:
- /v1/chat/completions  (the only request-serving endpoint)
- /healthz, /readyz, /metrics, /v1/usage
- The router, weight engine, refresh task, accounting queue, and DB

In dev (provider_mode=mock, secrets_mode=mock) this entire app runs without
needing real vendor credentials.
"""

from __future__ import annotations

import hmac
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as redis_async
from fastapi import FastAPI, Header, HTTPException, Request, Response

from gateway.accounting import AccountingQueue
from gateway.auth import CallerResolver
from gateway.breaker import BreakerSet, BreakerState
from gateway.config import ConfigHolder, install_sighup_reload, load_config
from gateway.db import Database
from gateway.metrics import (
    ACCOUNTING_DROPPED,
    ATTEMPTS_TOTAL,
    BREAKER_STATE,
    BUCKET_REMAINING,
    COST_USD_TOTAL,
    REDIS_DOWN,
    REQUEST_LATENCY,
    REQUESTS_TOTAL,
    ROUTING_WEIGHT,
    render_metrics,
)
from gateway.models import (
    Caller,
    CandidateRef,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorBody,
)
from gateway.providers import build_vendors
from gateway.ratelimit import RedisTokenBucket
from gateway.redis_state import RedisState
from gateway.router import Router, RouterError, RouterErrorKind
from gateway.routing.observe import Observer
from gateway.routing.refresh import RefreshTask
from gateway.routing.weights import (
    WeightEngine,
    budget_score,
    effective_weight,
    health_score,
)
from gateway.secrets import build_secrets_manager
from gateway.logging import configure_logging, get_logger


log = get_logger(__name__)

_METRICS_TOKEN_KEY = "GATEWAY_METRICS_TOKEN"


def _apply_env_overrides(
    cfg, *, provider_mode: str | None, secrets_mode: str | None
):
    """Re-validate config with env overrides applied.

    Uses Config.model_validate so that an invalid env value (e.g.
    GATEWAY_PROVIDER_MODE=reel) raises ValidationError at boot rather than
    causing a confusing failure later deep inside build_vendors.  (#9.2)
    """
    from gateway.models import Config

    if not provider_mode and not secrets_mode:
        return cfg
    data = cfg.model_dump()
    if provider_mode:
        data["provider_mode"] = provider_mode
    if secrets_mode:
        data["secrets_mode"] = secrets_mode
    return Config.model_validate(data)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("GATEWAY_LOG_LEVEL", "INFO"))

    config_path = os.environ.get("GATEWAY_CONFIG", "config.yaml")
    cfg = load_config(config_path)
    cfg = _apply_env_overrides(
        cfg,
        provider_mode=os.environ.get("GATEWAY_PROVIDER_MODE"),
        secrets_mode=os.environ.get("GATEWAY_SECRETS_MODE"),
    )
    holder = ConfigHolder(cfg, source_path=config_path)
    install_sighup_reload(holder)

    redis_url = os.environ.get("GATEWAY_REDIS_URL", "redis://localhost:6379/0")

    # #2.3: In real mode, GATEWAY_DB_DSN must be set explicitly — a missing
    # env var should fail loudly at startup rather than connecting to the dev
    # default (postgres://gateway:gateway@localhost) in a prod environment.
    db_dsn = os.environ.get("GATEWAY_DB_DSN", "")
    if cfg.provider_mode == "real" and not db_dsn:
        raise RuntimeError(
            "GATEWAY_DB_DSN must be set when provider_mode is 'real'. "
            "Refusing to start with an implicit default DSN in production."
        )
    if not db_dsn:
        db_dsn = "postgres://gateway:gateway@localhost:5432/gateway"

    r = redis_async.from_url(redis_url, decode_responses=False)
    state = RedisState(r)
    await state.load_scripts()
    REDIS_DOWN.set(0)

    db = Database(dsn=db_dsn)
    await db.connect()

    try:
        count = await db.pool.fetchval("SELECT COUNT(*) FROM callers")
        if count == 0:
            log.warning(
                "callers table is empty — run ./scripts/setup.sh to seed callers"
            )
    except Exception as exc:
        log.warning("could not check callers table at boot: %s", exc)

    secrets = build_secrets_manager(cfg.secrets_mode)

    # In mock mode, seed the secrets manager from env vars so that tests and
    # dev environments can supply GATEWAY_METRICS_TOKEN (and vendor keys) via
    # environment without wiring a real secrets backend.
    if cfg.secrets_mode == "mock":
        from gateway.secrets import MockSecretsManager

        assert isinstance(secrets, MockSecretsManager)
        for _key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            _METRICS_TOKEN_KEY,
            "GATEWAY_KEY_HASH_PEPPER",
        ):
            _val = os.environ.get(_key, "")
            if _val:
                secrets.set(_key, _val)

    pepper = secrets.get("GATEWAY_KEY_HASH_PEPPER")  # KeyError if missing -> startup crash, fail-loud

    vendors = build_vendors(cfg, secrets)

    bucket = RedisTokenBucket(state=state, limits=cfg.rate_limits)
    breakers = BreakerSet(state=state)
    observer = Observer(state=state, window_s=cfg.routing.health_window_s)
    engine = WeightEngine(routing=cfg.routing)
    refresh = RefreshTask(
        config=cfg,
        observer=observer,
        bucket=bucket,
        breakers=breakers,
        engine=engine,
        available_providers=set(vendors.keys()),
    )
    await refresh.tick()  # populate engine cache before serving
    refresh.start()

    rng_seed_env = cfg.routing.rng_seed_env
    rng_seed_raw = os.environ.get(rng_seed_env or "", "").strip()
    if rng_seed_raw:
        rng = random.Random(int(rng_seed_raw))
    else:
        rng = random.SystemRandom()

    accounting = AccountingQueue(writer=db)
    await accounting.start()

    router = Router(
        config=cfg,
        vendors=vendors,
        weight_engine=engine,
        bucket=bucket,
        observer=observer,
        rng=rng,
    )
    auth = CallerResolver(db=db, pepper=pepper)

    app.state.cfg = holder
    app.state.router = router
    app.state.auth = auth
    app.state.db = db
    app.state.accounting = accounting
    app.state.refresh = refresh
    app.state.engine = engine
    app.state.breakers = breakers
    app.state.bucket = bucket
    app.state.secrets = secrets

    try:
        yield
    finally:
        await refresh.stop()
        await accounting.stop()
        # ACCOUNTING_DROPPED is now incremented live in accounting._flush;
        # no extra bump needed here. (#6.3)
        await db.close()
        try:
            await r.aclose()
        except Exception:
            pass


app = FastAPI(title="Internal LLM Gateway", lifespan=lifespan)


# ---------------------------------------------------------------- helpers


async def _resolve_caller(
    request: Request, authorization: str | None
) -> Caller:
    auth: CallerResolver = request.app.state.auth
    caller = await auth.resolve_bearer(authorization)
    if caller is None:
        raise HTTPException(
            status_code=401,
            detail={"type": "auth", "message": "invalid or missing API key"},
        )
    return caller


# ---------------------------------------------------------------- endpoints


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request) -> dict[str, Any]:
    # Light readiness check — engine cache populated and DB pool open.
    return {"status": "ready", "tiers": list(request.app.state.cfg.value.tiers.keys())}


@app.get("/metrics")
async def metrics(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    # #3.3: Require Authorization: Bearer <GATEWAY_METRICS_TOKEN>.
    # Token is read from SecretsManager; if absent, deny (fail-closed).
    # Constant-time comparison prevents timing-oracle attacks.
    sm = request.app.state.secrets
    _unauth = Response(
        content='{"detail":"metrics auth required"}',
        status_code=401,
        media_type="application/json",
    )
    if not sm.has(_METRICS_TOKEN_KEY):
        return _unauth
    expected = sm.get(_METRICS_TOKEN_KEY)
    provided = ""
    if authorization and authorization.startswith("Bearer "):
        provided = authorization[len("Bearer "):].strip()
    if not hmac.compare_digest(expected, provided):
        return _unauth

    _refresh_observability_gauges(request)
    body, ctype = render_metrics()
    return Response(content=body, media_type=ctype)


def _refresh_observability_gauges(request: Request) -> None:
    eng: WeightEngine = request.app.state.engine
    cfg = request.app.state.cfg.value
    for tier_cands in cfg.tiers.values():
        for t in tier_cands:
            ref = CandidateRef(provider=t.provider, model=t.model)
            sig = eng.signals_for(ref)
            if sig is None:
                continue
            # budget_score / effective_weight / health_score are hoisted to
            # module top — no per-scrape re-import. (#8.2)
            h = health_score(
                error_rate=sig.error_rate,
                mean_latency_s=sig.mean_latency_s,
                target_latency_s=cfg.routing.target_latency_s,
            )
            b = budget_score(
                rpm_remaining=sig.rpm_remaining,
                rpm_cap=sig.rpm_cap,
                tpm_remaining=sig.tpm_remaining,
                tpm_cap=sig.tpm_cap,
            )
            w = effective_weight(
                base=sig.base_weight,
                health=h,
                budget=b,
                breaker=sig.breaker,
                floor=cfg.routing.min_weight_floor,
            )
            ROUTING_WEIGHT.labels(provider=t.provider, model=t.model).set(w)
            BREAKER_STATE.labels(provider=t.provider, model=t.model).set(
                {
                    BreakerState.CLOSED: 0,
                    BreakerState.HALF_OPEN: 1,
                    BreakerState.OPEN: 2,
                }[sig.breaker]
            )
            BUCKET_REMAINING.labels(
                provider=t.provider, model=t.model, dim="rpm"
            ).set(sig.rpm_remaining)
            BUCKET_REMAINING.labels(
                provider=t.provider, model=t.model, dim="tpm"
            ).set(sig.tpm_remaining)


@app.get("/v1/usage")
async def v1_usage(
    request: Request,
    authorization: str | None = Header(default=None),
    caller: str | None = None,  # noqa: ARG001 — ignored; kept for API compat
):
    # #3.2: Always use the authenticated caller's identity; never trust the
    # query-param caller.  Admin-scope querying (querying another caller's
    # usage) is future work — requires an is_admin column on callers.
    resolved = await _resolve_caller(request, authorization)
    db: Database = request.app.state.db
    summary = await db.usage_summary(caller=resolved.name)
    return {"items": summary}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
) -> ChatCompletionResponse:
    caller = await _resolve_caller(request, authorization)

    # Per-caller daily cap check.
    if caller.daily_token_cap is not None:
        db: Database = request.app.state.db
        used = await db.caller_tokens_used_today(caller.name)
        if used >= caller.daily_token_cap:
            raise HTTPException(
                status_code=429,
                detail={
                    "type": "caller_rate_limit",
                    "message": "daily token cap exhausted",
                    "retryable": False,
                },
            )

    router: Router = request.app.state.router
    accounting: AccountingQueue = request.app.state.accounting

    t0 = time.monotonic()
    try:
        result = await router.route(body, caller)
    except RouterError as e:
        elapsed = time.monotonic() - t0
        outcome = e.kind.value
        REQUESTS_TOTAL.labels(
            caller=caller.name, tier=body.model, outcome=outcome
        ).inc()
        REQUEST_LATENCY.labels(tier=body.model, outcome=outcome).observe(elapsed)
        status = _http_status_for(e.kind)
        raise HTTPException(status_code=status, detail=e.body.model_dump()) from e

    elapsed = time.monotonic() - t0
    REQUESTS_TOTAL.labels(
        caller=caller.name, tier=body.model, outcome="ok"
    ).inc()
    REQUEST_LATENCY.labels(tier=body.model, outcome="ok").observe(elapsed)

    for a in result.attempts:
        ATTEMPTS_TOTAL.labels(
            provider=a.provider, model=a.model, status=a.status
        ).inc()
        if a.status == "ok":
            COST_USD_TOTAL.labels(
                caller=caller.name, tier=body.model, provider=a.provider
            ).inc(a.cost_usd)
        accounting.enqueue(a)

    return result.response


def _http_status_for(kind: RouterErrorKind) -> int:
    return {
        RouterErrorKind.INVALID_REQUEST: 400,
        RouterErrorKind.AUTH: 401,
        RouterErrorKind.UPSTREAM_UNAVAILABLE: 503,
        RouterErrorKind.DEADLINE_EXCEEDED: 504,
    }[kind]
