"""All Pydantic models for the LLM gateway.

By convention this is the single home for Pydantic `BaseModel` subclasses in
the project (config schema, OpenAI-compatible wire shapes, internal DTOs).
Non-Pydantic types (ABCs, Protocols, plain dataclasses, enums that aren't
data models) belong in their own modules.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal

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

# ---------------------------------------------------------------- Constants

_CALLER_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

# Annotated type reused across CallerEntry, Caller, and AttemptRecord.caller
CallerName = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]


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


class RateLimitEntry(BaseModel):
    """Per-minute fleet-wide rate limits."""

    rpm: PositiveInt
    tpm: PositiveInt


class TierEntry(BaseModel):
    """One candidate in a tier's candidate list."""

    provider: str
    model: str
    weight: NonNegativeFloat
    rate_limits: RateLimitEntry


class TierConfig(BaseModel):
    """Configuration for one logical tier (e.g. 'fast', 'smart')."""

    candidates: list[TierEntry]


class CallerEntry(BaseModel):
    """One internal backend authorized to call the gateway."""

    name: CallerName
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
    """Top-level gateway configuration — loaded from Postgres (not YAML)."""

    model_config = ConfigDict(extra="forbid")

    provider_mode: ProviderMode
    secrets_mode: SecretsMode
    tiers: dict[str, TierConfig]
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    callers: list[CallerEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_validate_candidates_have_rate_limits(self) -> "Config":
        # Pydantic already enforces the RateLimitEntry shape inside TierEntry.
        # This validator is a final consistency pass — currently a no-op beyond
        # the structural check, kept for extension points.
        return self


# ---------------------------------------------------------------- Wire (OpenAI-compatible)


Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    role: Role
    content: str = Field(max_length=200_000)


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
    messages: list[Message] = Field(min_length=1, max_length=512)
    max_tokens: int = Field(default=1024, gt=0, le=16384)
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

    @field_validator("metadata")
    @classmethod
    def _validate_metadata_bounds(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        if v is None:
            return v
        if len(v) > 16:
            raise ValueError(
                f"metadata may have at most 16 entries, got {len(v)}"
            )
        for key, value in v.items():
            if len(key) > 64:
                raise ValueError(
                    f"metadata key {key!r} exceeds 64-character limit"
                )
            if len(value) > 256:
                raise ValueError(
                    f"metadata value for key {key!r} exceeds 256-character limit"
                )
        return v

    @model_validator(mode="after")
    def _validate_aggregate_content_size(self) -> "ChatCompletionRequest":
        total = sum(len(m.content) for m in self.messages)
        if total >= 1_000_000:
            raise ValueError(
                f"aggregate message content size {total} exceeds 1,000,000 characters"
            )
        return self


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
    caller: CallerName
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
    client_trace_id: str | None = Field(default=None, max_length=128)


class Caller(BaseModel):
    """Identity + policy for an internal caller, hydrated from config + DB."""

    name: CallerName
    daily_token_cap: NonNegativeInt | None = None
    enabled: bool = True
