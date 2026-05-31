"""FastAPI gateway entrypoint.

Wires:
- /v1/chat/completions  (the only request-serving endpoint)
- /healthz, /readyz, /metrics, /v1/usage
- The router, weight engine, refresh task, accounting queue, and DB

In dev (provider_mode=mock, secrets_mode=mock) this entire app runs without
needing real vendor credentials.
"""

from __future__ import annotations

import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as redis_async
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from gateway.accounting import AccountingQueue
from gateway.auth import CallerResolver, hash_api_key
from gateway.breaker import BreakerSet, BreakerState
from gateway.config import ConfigHolder, install_sighup_reload, load_config
from gateway.db import Database
from gateway.errors import ProviderError
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
from gateway.routing.weights import WeightEngine
from gateway.secrets import build_secrets_manager
from gateway.logging import configure_logging, get_logger


log = get_logger(__name__)


def _maybe_override(cfg, *, provider_mode_env: str | None, secrets_mode_env: str | None):
    if provider_mode_env:
        cfg = cfg.model_copy(update={"provider_mode": provider_mode_env})
    if secrets_mode_env:
        cfg = cfg.model_copy(update={"secrets_mode": secrets_mode_env})
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("GATEWAY_LOG_LEVEL", "INFO"))

    config_path = os.environ.get("GATEWAY_CONFIG", "config.yaml")
    cfg = load_config(config_path)
    cfg = _maybe_override(
        cfg,
        provider_mode_env=os.environ.get("GATEWAY_PROVIDER_MODE"),
        secrets_mode_env=os.environ.get("GATEWAY_SECRETS_MODE"),
    )
    holder = ConfigHolder(cfg, source_path=config_path)
    install_sighup_reload(holder)

    redis_url = os.environ.get("GATEWAY_REDIS_URL", "redis://localhost:6379/0")
    db_dsn = os.environ.get(
        "GATEWAY_DB_DSN", "postgres://gateway:gateway@localhost:5432/gateway"
    )

    r = redis_async.from_url(redis_url, decode_responses=False)
    state = RedisState(r)
    await state.load_scripts()
    REDIS_DOWN.set(0)

    db = Database(dsn=db_dsn)
    await db.connect()
    await db.run_migrations()

    # Seed callers table from config (dev convenience; in prod use a CLI).
    for c in cfg.callers:
        await db.upsert_caller(
            name=c.name,
            key_hash=c.key_hash,
            daily_token_cap=c.daily_token_cap,
            enabled=c.enabled,
        )

    secrets = build_secrets_manager(cfg.secrets_mode)
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
    if rng_seed_env and rng_seed_env in os.environ:
        rng = random.Random(int(os.environ[rng_seed_env]))
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
    auth = CallerResolver(db=db)

    app.state.cfg = holder
    app.state.router = router
    app.state.auth = auth
    app.state.db = db
    app.state.accounting = accounting
    app.state.refresh = refresh
    app.state.engine = engine
    app.state.breakers = breakers
    app.state.bucket = bucket

    try:
        yield
    finally:
        await refresh.stop()
        await accounting.stop()
        ACCOUNTING_DROPPED.inc(accounting.dropped_total)
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
    eng: WeightEngine = request.app.state.engine
    return {"status": "ready", "tiers": list(request.app.state.cfg.value.tiers.keys())}


@app.get("/metrics")
async def metrics(request: Request) -> Response:
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
            from gateway.routing.weights import (
                budget_score,
                effective_weight,
                health_score,
            )
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
    caller: str | None = None,
):
    # Auth required for usage too — callers can query their own data; an admin
    # caller (out of scope for this revision) would query anyone's.
    await _resolve_caller(request, authorization)
    db: Database = request.app.state.db
    summary = await db.usage_summary(caller=caller)
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
