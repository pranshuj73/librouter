"""Adaptive failover router.

The hot path of the gateway. Picks a candidate by weighted-random selection
(from `routing.weights.WeightEngine`), respects the global deadline (10s
default), excludes failed candidates from the next pick, and absorbs all
retryable provider errors.

Records each attempt's outcome into the observation window so future weight
refreshes reflect the freshest health signal.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from gateway.errors import (
    AuthError,
    BadRequest,
    ContentFiltered,
    ProviderError,
    RateLimited,
    Timeout,
    Transient5xx,
    caller_error_for,
)
from gateway.models import (
    AttemptRecord,
    Caller,
    CandidateRef,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatParams,
    Choice,
    Config,
    ErrorBody,
    Message,
    ProviderErrorKind,
    Usage,
)
from gateway.providers.base import Vendor
from gateway.ratelimit import RedisTokenBucket, estimate_tokens
from gateway.routing.observe import Observer
from gateway.routing.weights import WeightEngine


log = logging.getLogger(__name__)


class RouterErrorKind(str, Enum):
    INVALID_REQUEST = "invalid_request"
    AUTH = "auth"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(slots=True)
class RouterError(Exception):
    kind: RouterErrorKind
    body: ErrorBody
    tried: list[tuple[CandidateRef, str]]

    def __str__(self) -> str:  # pragma: no cover - debug aid only
        return f"{self.kind.value}: {self.body.message}"


@dataclass(slots=True)
class RouterResult:
    response: ChatCompletionResponse
    attempts: list[AttemptRecord]


_STATUS_FOR_ERROR_KIND: dict[type[ProviderError], str] = {
    RateLimited: "rate_limited",
    Transient5xx: "transient_5xx",
    Timeout: "timeout",
    BadRequest: "bad_request",
    AuthError: "auth",
    ContentFiltered: "content_filtered",
}


def default_clock_s() -> float:
    return time.monotonic()


class Router:
    def __init__(
        self,
        *,
        config: Config,
        vendors: dict[str, Vendor],
        weight_engine: WeightEngine,
        bucket: RedisTokenBucket,
        observer: Observer,
        rng: random.Random,
        deadline_clock_s: Callable[[], float] = default_clock_s,
        total_budget_s: float = 10.0,
        per_attempt_max_s: float = 8.0,
        deadline_buffer_s: float = 0.5,
    ) -> None:
        self._cfg = config
        self._vendors = vendors
        self._engine = weight_engine
        self._bucket = bucket
        self._obs = observer
        self._rng = rng
        self._now = deadline_clock_s
        self._total_budget_s = total_budget_s
        self._per_attempt_max_s = per_attempt_max_s
        self._buffer_s = deadline_buffer_s

    # ---------------------------------------------------------------- main

    async def route(
        self, req: ChatCompletionRequest, caller: Caller
    ) -> RouterResult:
        tier = req.model
        if tier not in self._cfg.tiers:
            raise RouterError(
                kind=RouterErrorKind.INVALID_REQUEST,
                body=ErrorBody(
                    type="invalid_request",
                    message=f"unknown tier {tier!r}",
                    retryable=False,
                ),
                tried=[],
            )

        deadline = self._now() + self._total_budget_s
        exclude: set[CandidateRef] = set()
        tried: list[tuple[CandidateRef, str]] = []
        attempts: list[AttemptRecord] = []

        params = ChatParams(
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
        )
        prompt_chars = sum(len(m.content) for m in req.messages)
        est = estimate_tokens(prompt_chars, req.max_tokens)
        request_id = (req.metadata or {}).get("request_id", "")

        while True:
            remaining = deadline - self._now()
            if remaining < 1.5:
                if attempts:
                    raise RouterError(
                        kind=RouterErrorKind.DEADLINE_EXCEEDED,
                        body=ErrorBody(
                            type="deadline_exceeded",
                            message="ran out of budget mid-failover",
                            retryable=True,
                        ),
                        tried=tried,
                    )
                break

            cand = self._engine.pick(
                self._cfg.tiers[tier], exclude=exclude, rng=self._rng
            )
            if cand is None:
                break

            ok, _, _ = await self._bucket.try_acquire(
                cand.provider, cand.model, request_tokens=est
            )
            if not ok:
                tried.append((cand, "bucket_empty"))
                exclude.add(cand)
                continue

            attempt_timeout = min(
                max(0.1, remaining - self._buffer_s), self._per_attempt_max_s
            )
            t0 = self._now()
            vendor = self._vendors.get(cand.provider)
            if vendor is None:
                tried.append((cand, "vendor_missing"))
                exclude.add(cand)
                continue
            try:
                result = await vendor.chat(
                    cand.model, req.messages, params, attempt_timeout
                )
            except ProviderError as e:
                elapsed = self._now() - t0
                status = _STATUS_FOR_ERROR_KIND.get(type(e), "transient_5xx")
                attempts.append(
                    self._record(
                        request_id=request_id,
                        caller=caller.name,
                        tier=tier,
                        cand=cand,
                        attempt_idx=len(attempts),
                        latency_s=elapsed,
                        status=status,
                    )
                )
                await self._obs.record_failure(cand, kind=type(e).__name__)

                if isinstance(e, (BadRequest, AuthError, ContentFiltered)):
                    http_status, body = caller_error_for(e)
                    raise RouterError(
                        kind=RouterErrorKind.INVALID_REQUEST
                        if http_status == 400
                        else RouterErrorKind.AUTH,
                        body=body,
                        tried=tried + [(cand, status)],
                    ) from e

                tried.append((cand, status))
                exclude.add(cand)
                continue

            elapsed = self._now() - t0
            await self._obs.record_success(cand, latency_s=elapsed)
            rec = self._record(
                request_id=request_id,
                caller=caller.name,
                tier=tier,
                cand=cand,
                attempt_idx=len(attempts),
                latency_s=elapsed,
                status="ok",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                vendor_req_id=result.vendor_request_id,
            )
            attempts.append(rec)
            response = ChatCompletionResponse(
                id=request_id or rec.vendor_req_id or "req",
                model=tier,
                choices=[
                    Choice(
                        index=0,
                        message=Message(role="assistant", content=result.text),
                        finish_reason=result.finish_reason,
                    )
                ],
                usage=Usage(
                    prompt_tokens=result.input_tokens,
                    completion_tokens=result.output_tokens,
                    total_tokens=result.input_tokens + result.output_tokens,
                ),
            )
            return RouterResult(response=response, attempts=attempts)

        raise RouterError(
            kind=RouterErrorKind.UPSTREAM_UNAVAILABLE,
            body=ErrorBody(
                type="upstream_unavailable",
                message="all candidates exhausted",
                retryable=True,
            ),
            tried=tried,
        )

    # ---------------------------------------------------------------- helpers

    def _record(
        self,
        *,
        request_id: str,
        caller: str,
        tier: str,
        cand: CandidateRef,
        attempt_idx: int,
        latency_s: float,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        vendor_req_id: str | None = None,
    ) -> AttemptRecord:
        price = self._cfg.prices.get(cand.key())
        if price is None:
            cost = 0.0
        else:
            cost = (
                input_tokens * price.input + output_tokens * price.output
            ) / 1_000_000
        return AttemptRecord(
            request_id=request_id or "req",
            caller=caller,
            tier=tier,
            provider=cand.provider,
            model=cand.model,
            attempt_idx=attempt_idx,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=max(0, int(latency_s * 1000)),
            status=status,
            vendor_req_id=vendor_req_id,
        )
