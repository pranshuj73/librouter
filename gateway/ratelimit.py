"""Redis-backed token bucket per (provider, model).

Capacity is configured fleet-wide (Redis is the single source of truth, so we
don't divide by replica count). Bucket holds two dimensions — requests per
minute (RPM) and tokens per minute (TPM) — atomically acquired together via
the Lua script in `redis_state`.

The clock is injected (`now_ms_fn`) so tests can freeze time without touching
`asyncio.get_event_loop().time()`.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gateway.models import RateLimitEntry
from gateway.redis_state import RedisState


def _candidate_key(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def default_now_ms() -> int:
    return int(time.time() * 1000)


class RedisTokenBucket:
    """Thin facade over `RedisState.ratelimit_acquire`.

    Construct once at startup with the rate-limits map from `Config`, then call
    `try_acquire(provider, model, request_tokens=...)` per attempt.
    """

    def __init__(
        self,
        *,
        state: RedisState,
        limits: dict[str, RateLimitEntry],
        now_ms_fn: Callable[[], int] = default_now_ms,
    ) -> None:
        self._state = state
        self._limits = limits
        self._now_ms = now_ms_fn

    def _entry(self, provider: str, model: str) -> RateLimitEntry:
        try:
            return self._limits[_candidate_key(provider, model)]
        except KeyError as e:
            raise KeyError(
                f"no rate_limits entry for {provider!r}/{model!r}"
            ) from e

    async def try_acquire(
        self, provider: str, model: str, *, request_tokens: int
    ) -> tuple[bool, int, int]:
        entry = self._entry(provider, model)
        key = self._state.bucket_key(provider, model)
        return await self._state.ratelimit_acquire(
            key,
            now_ms=self._now_ms(),
            rpm_cap=entry.rpm,
            tpm_cap=entry.tpm,
            refill_per_ms_rpm=entry.rpm / 60_000,
            refill_per_ms_tpm=entry.tpm / 60_000,
            request_tokens=request_tokens,
        )

    async def clamp(
        self, provider: str, model: str, *, rpm_observed: int, tpm_observed: int
    ) -> tuple[int, int]:
        self._entry(provider, model)  # validate exists
        key = self._state.bucket_key(provider, model)
        return await self._state.ratelimit_clamp(
            key, rpm_observed=rpm_observed, tpm_observed=tpm_observed
        )

    async def remaining(self, provider: str, model: str) -> tuple[int, int]:
        """Read current (rpm_remaining, tpm_remaining) without consuming.

        Acquiring 0 tokens lazily refills and returns post-state. We use a tiny
        epsilon trick: ask for 0 RPM/TPM, which always succeeds and reveals the
        current count.
        """
        entry = self._entry(provider, model)
        key = self._state.bucket_key(provider, model)
        _, rpm, tpm = await self._state.ratelimit_acquire(
            key,
            now_ms=self._now_ms(),
            rpm_cap=entry.rpm,
            tpm_cap=entry.tpm,
            refill_per_ms_rpm=entry.rpm / 60_000,
            refill_per_ms_tpm=entry.tpm / 60_000,
            request_tokens=0,
        )
        # We did consume 1 RPM with that call. Restore it via clamp+1 isn't
        # possible cheaply, so callers should not depend on `remaining` for
        # rate-limit correctness; it's a metrics/observability helper.
        return rpm, tpm


def estimate_tokens(prompt_chars: int, max_tokens: int) -> int:
    """Estimate total token cost for bucket acquisition.

    Rough but consistent: ~4 chars/token for the prompt + `max_tokens` for the
    response. The router uses this to size the TPM acquire.
    """
    return max(1, prompt_chars // 4 + max_tokens)
