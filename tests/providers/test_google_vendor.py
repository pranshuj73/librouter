"""Contract test for the real Google Gemini adapter — SDK-level monkeypatching."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.errors import (
    AuthError,
    BadRequest,
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
