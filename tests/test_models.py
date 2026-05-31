"""Tests for gateway/models.py.

TDD step 1. Models must:
- Round-trip a realistic config.yaml shape
- Reject invalid configs:
  * unknown provider in a tier
  * negative price or weight
  * tier referencing a (provider, model) without a price entry
  * caller missing key_hash
- Reject ChatCompletionRequest with stream=true (v1 non-streaming)
- Accept logical tier names in the `model` field
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gateway.models import (
    AttemptRecord,
    Caller,
    CallerEntry,
    CandidateRef,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatParams,
    ChatResult,
    Choice,
    Config,
    ErrorBody,
    Message,
    PriceEntry,
    ProviderErrorKind,
    RateLimitEntry,
    RoutingConfig,
    TierEntry,
    Usage,
)


# ---------------------------------------------------------------- Config


def _valid_config_dict() -> dict:
    return {
        "provider_mode": "mock",
        "secrets_mode": "mock",
        "tiers": {
            "fast": [
                {"provider": "anthropic", "model": "haiku", "weight": 50},
                {"provider": "openai", "model": "gpt-mini", "weight": 50},
            ],
        },
        "routing": {
            "refresh_interval_ms": 1000,
            "health_window_s": 60,
            "target_latency_s": 3.0,
            "min_weight_floor": 0.02,
        },
        "prices": {
            "anthropic/haiku": {"input": 1.0, "output": 5.0},
            "openai/gpt-mini": {"input": 0.15, "output": 0.6},
        },
        "rate_limits": {
            "anthropic/haiku": {"rpm": 1000, "tpm": 100000},
            "openai/gpt-mini": {"rpm": 1000, "tpm": 100000},
        },
        "callers": [
            {"name": "svc-a", "key_hash": "sha256:abc", "daily_token_cap": 1000000},
        ],
    }


def test_config_round_trip():
    cfg = Config.model_validate(_valid_config_dict())
    assert cfg.provider_mode == "mock"
    assert cfg.secrets_mode == "mock"
    assert "fast" in cfg.tiers
    assert cfg.tiers["fast"][0].weight == 50
    assert cfg.routing.refresh_interval_ms == 1000
    assert cfg.prices["anthropic/haiku"].input == 1.0
    assert cfg.callers[0].name == "svc-a"


def test_config_rejects_negative_weight():
    d = _valid_config_dict()
    d["tiers"]["fast"][0]["weight"] = -1
    with pytest.raises(ValidationError):
        Config.model_validate(d)


def test_config_rejects_negative_price():
    d = _valid_config_dict()
    d["prices"]["anthropic/haiku"]["input"] = -0.1
    with pytest.raises(ValidationError):
        Config.model_validate(d)


def test_config_rejects_tier_candidate_missing_price():
    d = _valid_config_dict()
    d["tiers"]["fast"].append({"provider": "google", "model": "gemini", "weight": 25})
    # google/gemini has no price entry -> should fail cross-validation
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(d)
    assert "price" in str(exc.value).lower()


def test_config_rejects_tier_candidate_missing_rate_limit():
    d = _valid_config_dict()
    d["prices"]["google/gemini"] = {"input": 1.0, "output": 2.0}
    d["tiers"]["fast"].append({"provider": "google", "model": "gemini", "weight": 25})
    # rate_limits missing -> should fail
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(d)
    assert "rate" in str(exc.value).lower()


def test_config_rejects_unknown_provider_mode():
    d = _valid_config_dict()
    d["provider_mode"] = "bogus"
    with pytest.raises(ValidationError):
        Config.model_validate(d)


def test_caller_entry_requires_key_hash():
    with pytest.raises(ValidationError):
        CallerEntry.model_validate({"name": "x", "daily_token_cap": 100})


def test_tier_entry_validates_weight_non_negative():
    TierEntry(provider="openai", model="gpt-4o", weight=0)
    with pytest.raises(ValidationError):
        TierEntry(provider="openai", model="gpt-4o", weight=-1)


def test_price_entry_validates_non_negative():
    PriceEntry(input=0.0, output=0.0)
    with pytest.raises(ValidationError):
        PriceEntry(input=-0.01, output=1.0)


def test_rate_limit_entry_validates_positive():
    RateLimitEntry(rpm=1, tpm=1)
    with pytest.raises(ValidationError):
        RateLimitEntry(rpm=0, tpm=100)


def test_routing_config_defaults():
    rc = RoutingConfig()
    assert rc.refresh_interval_ms == 1000
    assert rc.health_window_s == 60
    assert rc.target_latency_s == 3.0
    assert rc.min_weight_floor == 0.02


# ---------------------------------------------------------------- Wire models


def _valid_chat_request_dict() -> dict:
    return {
        "model": "fast",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
        "max_tokens": 64,
        "temperature": 0.2,
        "stream": False,
    }


def test_chat_completion_request_accepts_tier_name():
    req = ChatCompletionRequest.model_validate(_valid_chat_request_dict())
    assert req.model == "fast"
    assert req.messages[0].role == "system"
    assert req.stream is False


def test_chat_completion_request_rejects_streaming_in_v1():
    d = _valid_chat_request_dict()
    d["stream"] = True
    with pytest.raises(ValidationError) as exc:
        ChatCompletionRequest.model_validate(d)
    assert "stream" in str(exc.value).lower()


def test_chat_completion_request_requires_messages():
    d = _valid_chat_request_dict()
    d["messages"] = []
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)


def test_chat_completion_request_max_tokens_must_be_positive():
    d = _valid_chat_request_dict()
    d["max_tokens"] = 0
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)


def test_chat_completion_response_round_trip():
    resp = ChatCompletionResponse(
        id="req-123",
        model="fast",
        choices=[
            Choice(
                index=0,
                message=Message(role="assistant", content="hello"),
                finish_reason="stop",
            )
        ],
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    j = resp.model_dump()
    assert j["choices"][0]["message"]["content"] == "hello"
    assert j["usage"]["total_tokens"] == 15


def test_error_body_shape():
    e = ErrorBody(type="invalid_request", message="bad", retryable=False)
    j = e.model_dump()
    assert j == {"type": "invalid_request", "message": "bad", "retryable": False}


# ---------------------------------------------------------------- Internal DTOs


def test_chat_params_defaults():
    p = ChatParams(max_tokens=128)
    assert p.max_tokens == 128
    assert p.temperature is None


def test_chat_result_round_trip():
    r = ChatResult(
        text="ok",
        finish_reason="stop",
        input_tokens=10,
        output_tokens=20,
        vendor_request_id="vrid-1",
    )
    assert r.input_tokens == 10
    assert r.output_tokens == 20


def test_candidate_ref_is_hashable_and_equal():
    a = CandidateRef(provider="openai", model="gpt-4o")
    b = CandidateRef(provider="openai", model="gpt-4o")
    c = CandidateRef(provider="openai", model="gpt-4o-mini")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    s = {a, b, c}
    assert len(s) == 2


def test_attempt_record_required_fields():
    rec = AttemptRecord(
        request_id="req-1",
        caller="svc-a",
        tier="fast",
        provider="openai",
        model="gpt-4o-mini",
        attempt_idx=0,
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.001,
        latency_ms=400,
        status="ok",
        vendor_req_id="vrid-1",
    )
    assert rec.attempt_idx == 0
    assert rec.cost_usd == 0.001


def test_caller_internal_dto():
    c = Caller(name="svc-a", daily_token_cap=1_000_000, enabled=True)
    assert c.name == "svc-a"
    assert c.enabled is True


def test_provider_error_kind_enum_values():
    assert ProviderErrorKind.RATE_LIMITED.value == "rate_limited"
    assert ProviderErrorKind.TRANSIENT_5XX.value == "transient_5xx"
    assert ProviderErrorKind.TIMEOUT.value == "timeout"
    assert ProviderErrorKind.BAD_REQUEST.value == "bad_request"
    assert ProviderErrorKind.AUTH.value == "auth"
    assert ProviderErrorKind.CONTENT_FILTERED.value == "content_filtered"
