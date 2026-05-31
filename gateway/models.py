"""All Pydantic models for the LLM gateway.

By convention this is the single home for Pydantic `BaseModel` subclasses in
the project (config schema, OpenAI-compatible wire shapes, internal DTOs).
Non-Pydantic types (ABCs, Protocols, plain dataclasses, enums that aren't
data models) belong in their own modules.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------- Enums


class ProviderErrorKind(str, Enum):
    """Normalized error taxonomy emitted by Vendor adapters."""

    RATE_LIMITED = "rate_limited"
    TRANSIENT_5XX = "transient_5xx"
    TIMEOUT = "timeout"
    BAD_REQUEST = "bad_request"
    AUTH = "auth"
    CONTENT_FILTERED = "content_filtered"


ProviderMode = Literal["mock", "real"]
SecretsMode = Literal["mock", "env"]


# ---------------------------------------------------------------- Config


class TierEntry(BaseModel):
    """One candidate in a tier's candidate list."""

    provider: str
    model: str
    weight: NonNegativeFloat


class PriceEntry(BaseModel):
    """USD per 1M tokens, per (provider, model)."""

    input: NonNegativeFloat
    output: NonNegativeFloat


class RateLimitEntry(BaseModel):
    """Per-minute fleet-wide rate limits."""

    rpm: PositiveInt
    tpm: PositiveInt


class CallerEntry(BaseModel):
    """One internal backend authorized to call the gateway."""

    name: str
    key_hash: str
    daily_token_cap: NonNegativeInt | None = None
    enabled: bool = True


class RoutingConfig(BaseModel):
    refresh_interval_ms: PositiveInt = 1000
    health_window_s: PositiveInt = 60
    target_latency_s: float = Field(default=3.0, gt=0.0)
    min_weight_floor: NonNegativeFloat = 0.02
    rng_seed_env: str | None = None


class Config(BaseModel):
    """Top-level YAML schema."""

    model_config = ConfigDict(extra="forbid")

    provider_mode: ProviderMode
    secrets_mode: SecretsMode
    tiers: dict[str, list[TierEntry]]
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    prices: dict[str, PriceEntry]
    rate_limits: dict[str, RateLimitEntry]
    callers: list[CallerEntry]

    @model_validator(mode="after")
    def _cross_validate_candidates_have_pricing_and_limits(self) -> "Config":
        for tier_name, candidates in self.tiers.items():
            for cand in candidates:
                key = f"{cand.provider}/{cand.model}"
                if key not in self.prices:
                    raise ValueError(
                        f"tier {tier_name!r} candidate {key!r} has no price entry"
                    )
                if key not in self.rate_limits:
                    raise ValueError(
                        f"tier {tier_name!r} candidate {key!r} has no rate_limits entry"
                    )
        return self


# ---------------------------------------------------------------- Wire (OpenAI-compatible)


Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    role: Role
    content: str


class Usage(BaseModel):
    prompt_tokens: NonNegativeInt
    completion_tokens: NonNegativeInt
    total_tokens: NonNegativeInt


class Choice(BaseModel):
    index: NonNegativeInt
    message: Message
    finish_reason: str | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-shaped request. `model` is a logical tier name (e.g. 'fast')."""

    model: str
    messages: list[Message] = Field(min_length=1)
    max_tokens: PositiveInt = 1024
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stream: bool = False
    metadata: dict[str, str] | None = None

    @field_validator("stream")
    @classmethod
    def _no_streaming_in_v1(cls, v: bool) -> bool:
        if v:
            raise ValueError("stream=true is not supported in v1 (non-streaming only)")
        return v


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    model: str
    choices: list[Choice]
    usage: Usage


class ErrorBody(BaseModel):
    type: str
    message: str
    retryable: bool = False


# ---------------------------------------------------------------- Internal DTOs


class ChatParams(BaseModel):
    """Generation params handed to a Vendor adapter."""

    max_tokens: PositiveInt
    temperature: float | None = None
    top_p: float | None = None


class ChatResult(BaseModel):
    """Normalized vendor response."""

    text: str
    finish_reason: str | None = None
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    vendor_request_id: str | None = None


class CandidateRef(BaseModel):
    """Hashable handle for a (provider, model) pair."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str

    def key(self) -> str:
        return f"{self.provider}/{self.model}"


class AttemptRecord(BaseModel):
    """One row in the requests table — every attempt is recorded, not just the winner."""

    request_id: str
    caller: str
    tier: str
    provider: str
    model: str
    attempt_idx: NonNegativeInt
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cost_usd: NonNegativeFloat = 0.0
    latency_ms: NonNegativeInt
    status: str
    vendor_req_id: str | None = None


class Caller(BaseModel):
    """Identity + policy for an internal caller, hydrated from config + DB."""

    name: str
    daily_token_cap: NonNegativeInt | None = None
    enabled: bool = True
