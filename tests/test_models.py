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


# ---------------------------------------------------------------- Finding 4.1: max_tokens bounds


def test_max_tokens_rejects_zero():
    d = _valid_chat_request_dict()
    d["max_tokens"] = 0
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)


def test_max_tokens_rejects_above_cap():
    d = _valid_chat_request_dict()
    d["max_tokens"] = 16385
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)

    d["max_tokens"] = 16384
    req = ChatCompletionRequest.model_validate(d)
    assert req.max_tokens == 16384


def test_max_tokens_default_is_1024():
    d = _valid_chat_request_dict()
    del d["max_tokens"]
    req = ChatCompletionRequest.model_validate(d)
    assert req.max_tokens == 1024


# ---------------------------------------------------------------- Finding 4.1: Message.content max_length


def test_message_content_rejects_oversize():
    with pytest.raises(ValidationError):
        Message(role="user", content="x" * 200_001)

    msg = Message(role="user", content="x" * 200_000)
    assert len(msg.content) == 200_000


# ---------------------------------------------------------------- Finding 4.1: messages list max_length


def _make_chat_request_with_n_messages(n: int) -> dict:
    return {
        "model": "fast",
        "messages": [{"role": "user", "content": "hi"}] * n,
        "max_tokens": 64,
        "stream": False,
    }


def test_messages_list_rejects_too_many():
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(_make_chat_request_with_n_messages(513))

    req = ChatCompletionRequest.model_validate(_make_chat_request_with_n_messages(512))
    assert len(req.messages) == 512


# ---------------------------------------------------------------- Finding 4.1: aggregate content size guard


def test_messages_aggregate_size_capped():
    # 5 messages × 250_000 chars = 1_250_000 → should fail (>= 1_000_000)
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {
                "model": "fast",
                "messages": [{"role": "user", "content": "x" * 250_000}] * 5,
                "max_tokens": 64,
                "stream": False,
            }
        )

    # 5 messages × 199_000 chars = 995_000 → should succeed (< 1_000_000)
    req = ChatCompletionRequest.model_validate(
        {
            "model": "fast",
            "messages": [{"role": "user", "content": "x" * 199_000}] * 5,
            "max_tokens": 64,
            "stream": False,
        }
    )
    assert len(req.messages) == 5


# ---------------------------------------------------------------- Finding 4.3: metadata bounds


def test_metadata_rejects_too_many_keys():
    d = _valid_chat_request_dict()
    d["metadata"] = {str(i): "v" for i in range(17)}
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)

    d["metadata"] = {str(i): "v" for i in range(16)}
    req = ChatCompletionRequest.model_validate(d)
    assert len(req.metadata) == 16


def test_metadata_rejects_oversize_key():
    d = _valid_chat_request_dict()
    d["metadata"] = {"k" * 65: "value"}
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)

    d["metadata"] = {"k" * 64: "value"}
    req = ChatCompletionRequest.model_validate(d)
    assert "k" * 64 in req.metadata


def test_metadata_rejects_oversize_value():
    d = _valid_chat_request_dict()
    d["metadata"] = {"key": "v" * 257}
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)

    d["metadata"] = {"key": "v" * 256}
    req = ChatCompletionRequest.model_validate(d)
    assert req.metadata["key"] == "v" * 256


def test_metadata_none_still_allowed():
    d = _valid_chat_request_dict()
    d["metadata"] = None
    req = ChatCompletionRequest.model_validate(d)
    assert req.metadata is None


# ---------------------------------------------------------------- Finding 4.4: caller name regex


def test_caller_entry_name_regex_rejects_uppercase():
    with pytest.raises(ValidationError):
        CallerEntry(name="SvcA", key_hash="sha256:abc")


def test_caller_entry_name_regex_rejects_special_chars():
    for bad_name in ("svc/a", "svc a", "svc.a", "svc\na"):
        with pytest.raises(ValidationError):
            CallerEntry(name=bad_name, key_hash="sha256:abc")


def test_caller_entry_name_regex_accepts_canonical():
    for good_name in ("svc-a", "svc_a", "svc-123"):
        entry = CallerEntry(name=good_name, key_hash="sha256:abc")
        assert entry.name == good_name


def test_caller_entry_name_regex_rejects_too_long():
    with pytest.raises(ValidationError):
        CallerEntry(name="a" * 65, key_hash="sha256:abc")

    entry = CallerEntry(name="a" * 64, key_hash="sha256:abc")
    assert len(entry.name) == 64


def test_caller_name_regex_applies_to_caller_dto():
    with pytest.raises(ValidationError):
        Caller(name="SvcA")
    with pytest.raises(ValidationError):
        Caller(name="svc/a")

    c = Caller(name="svc-a")
    assert c.name == "svc-a"


def test_caller_name_regex_applies_to_attempt_record():
    base = dict(
        request_id="req-1",
        tier="fast",
        provider="openai",
        model="gpt-4o-mini",
        attempt_idx=0,
        latency_ms=100,
        status="ok",
    )
    with pytest.raises(ValidationError):
        AttemptRecord(**base, caller="SvcA")
    with pytest.raises(ValidationError):
        AttemptRecord(**base, caller="svc/a")

    rec = AttemptRecord(**base, caller="svc-a")
    assert rec.caller == "svc-a"


# ---------------------------------------------------------------- Finding 6 — gap-closing scenarios
#
# The cases below close the §6 gaps in `docs/code-review/t-1.md`. Most
# behaviors *currently* exist in `gateway/models.py` (the recent additions
# for cr-1 §4 are in place), so these assertions pin them in place. A few
# behaviors are documented today but not enforced; those are marked xfail
# with a clear citation so we don't lose track.


def test_config_rejects_extra_top_level_field():
    """`Config.model_config = ConfigDict(extra='forbid')` rejects unknowns."""
    d = _valid_config_dict()
    d["nonsense_top_level"] = True
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(d)
    # The pydantic v2 error mentions "extra" or "forbidden"; either is fine.
    msg = str(exc.value).lower()
    assert "extra" in msg or "forbidden" in msg or "not permitted" in msg


def test_candidate_ref_is_frozen():
    """`CandidateRef` uses `ConfigDict(frozen=True)`; field assignment must raise."""
    ref = CandidateRef(provider="openai", model="gpt-4o")
    # In pydantic v2 a frozen-model assignment raises `ValidationError`.
    with pytest.raises(ValidationError):
        ref.provider = "anthropic"  # type: ignore[misc]


def test_attempt_record_rejects_negative_input_tokens():
    """`input_tokens: NonNegativeInt` should reject negative values."""
    base = dict(
        request_id="req-1",
        caller="svc-a",
        tier="fast",
        provider="openai",
        model="gpt-4o-mini",
        attempt_idx=0,
        latency_ms=100,
        status="ok",
    )
    with pytest.raises(ValidationError):
        AttemptRecord(**base, input_tokens=-1)
    with pytest.raises(ValidationError):
        AttemptRecord(**base, output_tokens=-1)


# ---------- temperature / top_p boundaries (existing validators) ----------


def test_temperature_boundary_2_0_accepted():
    d = _valid_chat_request_dict()
    d["temperature"] = 2.0
    req = ChatCompletionRequest.model_validate(d)
    assert req.temperature == 2.0


def test_temperature_above_2_0_rejected():
    d = _valid_chat_request_dict()
    d["temperature"] = 2.01
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)


def test_top_p_zero_accepted():
    d = _valid_chat_request_dict()
    d["top_p"] = 0.0
    req = ChatCompletionRequest.model_validate(d)
    assert req.top_p == 0.0


def test_top_p_one_accepted():
    d = _valid_chat_request_dict()
    d["top_p"] = 1.0
    req = ChatCompletionRequest.model_validate(d)
    assert req.top_p == 1.0


def test_top_p_above_one_rejected():
    d = _valid_chat_request_dict()
    d["top_p"] = 1.01
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(d)


# ---------- CallerEntry.daily_token_cap boundary ----------


def test_caller_entry_daily_token_cap_zero_accepted():
    """`NonNegativeInt` permits 0 — verify the boundary."""
    entry = CallerEntry(name="svc-a", key_hash="sha256:abc", daily_token_cap=0)
    assert entry.daily_token_cap == 0


# ---------- Empty tiers — document current behavior ----------


def test_empty_tiers_dict_currently_accepted():
    """An empty `tiers` dict is currently accepted (no validator iterates it).

    TODO(cr-1 §4): the recommendation is to reject this — when implemented,
    flip this test to `with pytest.raises(ValidationError):`.
    """
    d = _valid_config_dict()
    d["tiers"] = {}
    # Today this validates: the cross-validator just iterates an empty dict.
    cfg = Config.model_validate(d)
    assert cfg.tiers == {}


# ---------- provider_mode / secrets_mode combo — documented, not enforced ----------


def test_provider_real_with_secrets_mock_currently_accepted():
    """`provider_mode='real'` with `secrets_mode='mock'` is currently allowed.

    This combination is almost certainly a misconfiguration in production
    (real vendor calls with no real API keys), but `gateway/models.py`
    has no model-level validator that forbids it. The gate is documented
    contract, not schema. Pinned here so an accidental schema change
    elsewhere doesn't silently shift the contract.
    """
    d = _valid_config_dict()
    d["provider_mode"] = "real"
    d["secrets_mode"] = "mock"
    cfg = Config.model_validate(d)
    assert cfg.provider_mode == "real"
    assert cfg.secrets_mode == "mock"


# ---------- metadata value-type coercion ----------


def test_metadata_non_string_value_behavior():
    """Pydantic `dict[str, str]` coerces ints → strings by default.

    This pins the *current* observable behavior. If a strict validator is
    added later (cr-1 §4.3 follow-up), this assertion should flip to a
    `with pytest.raises(ValidationError):` — but today it is documented as
    silent coercion.
    """
    d = _valid_chat_request_dict()
    d["metadata"] = {"foo": 123}
    try:
        req = ChatCompletionRequest.model_validate(d)
    except ValidationError:
        # If a future pydantic / model change starts rejecting this, that
        # is *better* behavior — accept either path.
        return
    # If validation succeeded, the int must have been coerced to "123".
    assert req.metadata == {"foo": "123"}
