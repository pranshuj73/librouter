"""Async Redis client wrapper plus the Lua scripts the gateway depends on.

Three scripts:
* `ratelimit_lua` — atomic two-dimensional (RPM, TPM) token-bucket acquire with
  lazy refill. The clock is passed in via ARGV so tests can inject a deterministic
  `now_ms`.
* `clamp_lua` — opportunistically shrink the remaining-counter when the vendor's
  rate-limit headers report less than we think.
* `probe_lock_lua` — `SET key value NX EX ttl` packaged as Lua so we can use a
  single round-trip from the breaker.

Keys live under `gw:*`. Bucket state per `(provider, model)` is held in a hash
with the fields `rpm_remaining`, `tpm_remaining`, and `last_refill_ms`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import NoScriptError


# Two-dim token bucket. Lazy refill on every call; atomic acquire of 1 RPM and
# `request_tokens` TPM. Returns [ok (1|0), rpm_remaining, tpm_remaining].
RATELIMIT_LUA = """
local key = KEYS[1]
local now_ms        = tonumber(ARGV[1])
local rpm_cap       = tonumber(ARGV[2])
local tpm_cap       = tonumber(ARGV[3])
local refill_rpm_pm = tonumber(ARGV[4])  -- per ms
local refill_tpm_pm = tonumber(ARGV[5])  -- per ms
local req_tokens    = tonumber(ARGV[6])

local h = redis.call('HMGET', key, 'rpm_remaining', 'tpm_remaining', 'last_refill_ms')
local rpm_rem = tonumber(h[1])
local tpm_rem = tonumber(h[2])
local last_ms = tonumber(h[3])

if rpm_rem == nil then rpm_rem = rpm_cap end
if tpm_rem == nil then tpm_rem = tpm_cap end
if last_ms == nil then last_ms = now_ms end

local elapsed = now_ms - last_ms
if elapsed < 0 then elapsed = 0 end

rpm_rem = math.min(rpm_cap, rpm_rem + elapsed * refill_rpm_pm)
tpm_rem = math.min(tpm_cap, tpm_rem + elapsed * refill_tpm_pm)

local ok = 0
if rpm_rem >= 1 and tpm_rem >= req_tokens then
    rpm_rem = rpm_rem - 1
    tpm_rem = tpm_rem - req_tokens
    ok = 1
end

redis.call('HMSET', key, 'rpm_remaining', rpm_rem, 'tpm_remaining', tpm_rem, 'last_refill_ms', now_ms)
redis.call('PEXPIRE', key, 600000)

-- Lua can't return floats reliably across all Redis builds; round to int
return {ok, math.floor(rpm_rem), math.floor(tpm_rem)}
"""


# Shrink remaining counters if the vendor reports less than we hold.
CLAMP_LUA = """
local key = KEYS[1]
local rpm_observed = tonumber(ARGV[1])
local tpm_observed = tonumber(ARGV[2])

local h = redis.call('HMGET', key, 'rpm_remaining', 'tpm_remaining')
local rpm_cur = tonumber(h[1]) or rpm_observed
local tpm_cur = tonumber(h[2]) or tpm_observed

if rpm_observed < rpm_cur then rpm_cur = rpm_observed end
if tpm_observed < tpm_cur then tpm_cur = tpm_observed end

redis.call('HSET', key, 'rpm_remaining', rpm_cur, 'tpm_remaining', tpm_cur)
return {math.floor(rpm_cur), math.floor(tpm_cur)}
"""


@dataclass(slots=True)
class LoadedScripts:
    ratelimit: str
    clamp: str


class RedisState:
    """Thin wrapper that loads the Lua scripts once and runs them via EVALSHA."""

    KEY_BUCKET = "gw:bkt:{provider}:{model}"
    KEY_BREAKER = "gw:brk:{provider}:{model}"
    KEY_BREAKER_PROBE = "gw:brk:{provider}:{model}:probe"
    KEY_OBSERVE_SEC = "gw:obs:{provider}:{model}:{epoch_sec}"
    CHANNEL_BREAKER = "gw:brk-events"

    def __init__(self, redis: Redis) -> None:
        self._r = redis
        self._scripts: LoadedScripts | None = None

    @property
    def client(self) -> Redis:
        return self._r

    async def load_scripts(self) -> LoadedScripts:
        if self._scripts is None:
            self._scripts = LoadedScripts(
                ratelimit=await self._r.script_load(RATELIMIT_LUA),
                clamp=await self._r.script_load(CLAMP_LUA),
            )
        return self._scripts

    async def _eval(self, body: str, sha: str, numkeys: int, *args: Any) -> Any:
        """Run a Lua script via EVALSHA, falling back to EVAL on NOSCRIPT.

        Real Redis evicts script caches under memory pressure and fresh
        replicas come up with empty caches; the EVALSHA→EVAL fallback is the
        standard handling. Also lets us survive any script-cache quirks in
        the fake-redis-backed test environment.
        """
        try:
            return await self._r.evalsha(sha, numkeys, *args)
        except NoScriptError:
            return await self._r.eval(body, numkeys, *args)

    # ---------------------------------------------------------------- key helpers

    def bucket_key(self, provider: str, model: str) -> str:
        return self.KEY_BUCKET.format(provider=provider, model=model)

    def breaker_key(self, provider: str, model: str) -> str:
        return self.KEY_BREAKER.format(provider=provider, model=model)

    def breaker_probe_key(self, provider: str, model: str) -> str:
        return self.KEY_BREAKER_PROBE.format(provider=provider, model=model)

    def observe_key(self, provider: str, model: str, epoch_sec: int) -> str:
        return self.KEY_OBSERVE_SEC.format(
            provider=provider, model=model, epoch_sec=epoch_sec
        )

    # ---------------------------------------------------------------- script runners

    async def ratelimit_acquire(
        self,
        bucket_key: str,
        *,
        now_ms: int,
        rpm_cap: int,
        tpm_cap: int,
        refill_per_ms_rpm: float,
        refill_per_ms_tpm: float,
        request_tokens: int,
    ) -> tuple[bool, int, int]:
        scripts = await self.load_scripts()
        raw: list[Any] = await self._eval(
            RATELIMIT_LUA,
            scripts.ratelimit,
            1,
            bucket_key,
            str(now_ms),
            str(rpm_cap),
            str(tpm_cap),
            str(refill_per_ms_rpm),
            str(refill_per_ms_tpm),
            str(request_tokens),
        )
        return bool(int(raw[0])), int(raw[1]), int(raw[2])

    async def ratelimit_clamp(
        self, bucket_key: str, *, rpm_observed: int, tpm_observed: int
    ) -> tuple[int, int]:
        scripts = await self.load_scripts()
        raw: list[Any] = await self._eval(
            CLAMP_LUA,
            scripts.clamp,
            1,
            bucket_key,
            str(rpm_observed),
            str(tpm_observed),
        )
        return int(raw[0]), int(raw[1])

    async def acquire_probe_lock(
        self, probe_key: str, *, holder: str, ttl_s: int
    ) -> bool:
        """`SET NX EX` is already atomic in Redis — no Lua needed."""
        result = await self._r.set(probe_key, holder, nx=True, ex=ttl_s)
        return bool(result)
