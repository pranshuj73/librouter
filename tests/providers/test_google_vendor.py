"""Contract test for the real Google Gemini adapter — SDK-level monkeypatching."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.errors import (
    AuthError,
    BadRequest,
    ContentFiltered,
    RateLimited,
    Transient5xx,
)
from gateway.models import ChatParams, Message
from gateway.providers.base import Vendor
from gateway.providers.google import GoogleVendor
from gateway.secrets import MockSecretsManager


pytestmark = pytest.mark.asyncio


def _vendor() -> GoogleVendor:
    return GoogleVendor(MockSecretsManager({"GOOGLE_API_KEY": "g-test"}))


def _msg() -> list[Message]:
    return [Message(role="user", content="hi")]


def _params() -> ChatParams:
    return ChatParams(max_tokens=16)


def _stub_generate(*, returns=None, raises: Exception | None = None):
    async def _gen(**_kwargs):
        if raises is not None:
            raise raises
        return returns

    return _gen


def _success_payload() -> SimpleNamespace:
    candidate = SimpleNamespace(
        finish_reason="STOP",
        content=SimpleNamespace(parts=[SimpleNamespace(text="hi there")]),
    )
    return SimpleNamespace(
        text="hi there",
        candidates=[candidate],
        usage_metadata=SimpleNamespace(
            prompt_token_count=9, candidates_token_count=5
        ),
        response_id="rid-1",
    )


async def test_is_vendor_instance():
    assert isinstance(_vendor(), Vendor)


async def test_success_normalizes_to_chat_result(monkeypatch):
    v = _vendor()
    monkeypatch.setattr(
        v._client.aio.models, "generate_content",
        _stub_generate(returns=_success_payload()),
    )
    r = await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)
    assert r.text == "hi there"
    assert r.input_tokens == 9
    assert r.output_tokens == 5
    assert r.vendor_request_id == "rid-1"


class _StubGoogleResponse:
    """Minimal stand-in for genai's ReplayResponse used by APIError."""

    def __init__(self, error_message: str) -> None:
        self.body_segments = [{"error": {"message": error_message}}]


async def test_429_maps_to_rate_limited(monkeypatch):
    from google.genai import errors as genai_errors
    v = _vendor()
    exc = genai_errors.APIError(429, _StubGoogleResponse("rl"))
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(raises=exc)
    )
    with pytest.raises(RateLimited):
        await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)


async def test_500_maps_to_transient5xx(monkeypatch):
    from google.genai import errors as genai_errors
    v = _vendor()
    exc = genai_errors.APIError(500, _StubGoogleResponse("boom"))
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(raises=exc)
    )
    with pytest.raises(Transient5xx):
        await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)


async def test_401_maps_to_auth_error(monkeypatch):
    from google.genai import errors as genai_errors
    v = _vendor()
    exc = genai_errors.APIError(401, _StubGoogleResponse("bad key"))
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(raises=exc)
    )
    with pytest.raises(AuthError):
        await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)


async def test_400_maps_to_bad_request(monkeypatch):
    from google.genai import errors as genai_errors
    v = _vendor()
    exc = genai_errors.APIError(400, _StubGoogleResponse("bad"))
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(raises=exc)
    )
    with pytest.raises(BadRequest):
        await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)


# --------------------------------------------------------------------- t-1 §18
# Additions per docs/code-review/t-1.md §18 — uncovered branches in
# gateway/providers/google.py (SAFETY mapping, parts-fallback, missing usage,
# empty candidates, generic exception, assistant→model conversion).


async def test_finish_reason_with_safety_raises_content_filtered(monkeypatch):
    """Per t-1 §18 + gateway/providers/google.py:114-115."""
    v = _vendor()
    candidate = SimpleNamespace(
        finish_reason="STOP_SAFETY",
        content=SimpleNamespace(parts=[SimpleNamespace(text="blocked")]),
    )
    payload = SimpleNamespace(
        text="blocked",
        candidates=[candidate],
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=0),
        response_id="rid-safe",
    )
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(returns=payload)
    )
    with pytest.raises(ContentFiltered):
        await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)


async def test_resp_text_none_falls_back_to_candidate_parts(monkeypatch):
    """Per t-1 §18 — gateway/providers/google.py:98-106 parts-concat fallback."""
    v = _vendor()
    candidate = SimpleNamespace(
        finish_reason="STOP",
        content=SimpleNamespace(parts=[SimpleNamespace(text="from-parts")]),
    )
    payload = SimpleNamespace(
        text=None,
        candidates=[candidate],
        usage_metadata=SimpleNamespace(prompt_token_count=2, candidates_token_count=3),
        response_id="rid-parts",
    )
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(returns=payload)
    )
    r = await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)
    assert r.text == "from-parts"


async def test_usage_metadata_none_defaults_to_zero(monkeypatch):
    """Per t-1 §18 — `getattr(usage, ..., 0) or 0` with usage_metadata=None."""
    v = _vendor()
    candidate = SimpleNamespace(
        finish_reason="STOP",
        content=SimpleNamespace(parts=[SimpleNamespace(text="hi")]),
    )
    payload = SimpleNamespace(
        text="hi",
        candidates=[candidate],
        usage_metadata=None,
        response_id="rid-no-usage",
    )
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(returns=payload)
    )
    r = await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)
    assert r.input_tokens == 0
    assert r.output_tokens == 0


async def test_empty_candidates_yields_none_finish_reason(monkeypatch):
    """Per t-1 §18 — `candidates=[]` → no finish_reason, no exception."""
    v = _vendor()
    payload = SimpleNamespace(
        text="ok",
        candidates=[],
        usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
        response_id="rid-no-cand",
    )
    monkeypatch.setattr(
        v._client.aio.models, "generate_content", _stub_generate(returns=payload)
    )
    r = await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)
    assert r.finish_reason is None
    assert r.text == "ok"


async def test_unknown_sdk_exception_maps_to_transient5xx(monkeypatch):
    """Per t-1 §18 — defensive `except Exception` at google.py:93-94."""
    v = _vendor()
    monkeypatch.setattr(
        v._client.aio.models,
        "generate_content",
        _stub_generate(raises=ValueError("oops")),
    )
    with pytest.raises(Transient5xx):
        await v.chat("gemini-flash", _msg(), _params(), timeout_s=5.0)


async def test_convert_messages_maps_assistant_to_model_role(monkeypatch):
    """Per t-1 §18 — gateway/providers/google.py:_convert_messages role mapping."""
    v = _vendor()
    captured: dict = {}

    async def _gen(**kwargs):
        captured.update(kwargs)
        return _success_payload()

    monkeypatch.setattr(v._client.aio.models, "generate_content", _gen)

    messages = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    await v.chat("gemini-flash", messages, _params(), timeout_s=5.0)

    contents = captured["contents"]
    assert len(contents) == 2
    assert contents[0]["role"] == "user"
    assert contents[1]["role"] == "model"
