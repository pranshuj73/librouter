"""Contract test for the real OpenAI adapter.

Monkeypatches the SDK's `chat.completions.create` to verify the adapter's
error-taxonomy mapping and `ChatResult` shape without any network calls.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from gateway.errors import (
    AuthError,
    BadRequest,
    ContentFiltered,
    RateLimited,
    Timeout,
    Transient5xx,
)
from gateway.models import ChatParams, Message
from gateway.providers.base import Vendor
from gateway.providers.openai import OpenAIVendor
from gateway.secrets import MockSecretsManager


pytestmark = pytest.mark.asyncio


def _vendor() -> OpenAIVendor:
    return OpenAIVendor(MockSecretsManager({"OPENAI_API_KEY": "sk-test"}))


def _msg() -> list[Message]:
    return [Message(role="user", content="hi")]


def _params() -> ChatParams:
    return ChatParams(max_tokens=16)


def _stub_create(*, returns=None, raises: Exception | None = None):
    async def _create(**_kwargs):
        if raises is not None:
            raise raises
        return returns

    return _create


def _http_response(status: int, body: str = '{"error":{"message":"x"}}') -> httpx.Response:
    return httpx.Response(
        status_code=status,
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        content=body.encode(),
    )


def _stub_success_payload() -> SimpleNamespace:
    return SimpleNamespace(
        id="chatcmpl-xyz",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hello"),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10),
    )


async def test_is_vendor_instance():
    assert isinstance(_vendor(), Vendor)


async def test_success_normalizes_to_chat_result(monkeypatch):
    v = _vendor()
    monkeypatch.setattr(
        v._client.chat.completions, "create",
        _stub_create(returns=_stub_success_payload()),
    )
    r = await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)
    assert r.text == "hello"
    assert r.input_tokens == 7
    assert r.output_tokens == 3
    assert r.vendor_request_id == "chatcmpl-xyz"
    assert r.finish_reason == "stop"


async def test_429_maps_to_rate_limited(monkeypatch):
    v = _vendor()
    exc = RateLimitError(message="rl", response=_http_response(429), body=None)
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(RateLimited):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


async def test_500_maps_to_transient5xx(monkeypatch):
    v = _vendor()
    exc = InternalServerError(message="boom", response=_http_response(500), body=None)
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(Transient5xx):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


async def test_401_maps_to_auth_error(monkeypatch):
    v = _vendor()
    exc = AuthenticationError(message="Incorrect API key provided", response=_http_response(401), body=None)
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(AuthError) as exc_info:
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)
    # Vendor SDK message must NOT appear in the public ProviderError string (#4.2).
    assert "Incorrect API key" not in str(exc_info.value)


async def test_400_maps_to_bad_request(monkeypatch):
    v = _vendor()
    exc = BadRequestError(message="bad", response=_http_response(400), body=None)
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(BadRequest):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


async def test_timeout_maps_to_timeout(monkeypatch):
    v = _vendor()
    exc = APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(Timeout):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


async def test_connection_error_maps_to_transient5xx(monkeypatch):
    v = _vendor()
    exc = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(Transient5xx):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


async def test_apistatuserror_503_maps_to_transient5xx(monkeypatch):
    v = _vendor()
    exc = APIStatusError(message="x", response=_http_response(503), body=None)
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(Transient5xx):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


# --------------------------------------------------------------------- t-1 §16
# Additions per docs/code-review/t-1.md §16 — uncovered branches in
# gateway/providers/openai.py (content_filter, empty content, missing usage,
# unmapped APIStatusError code).


async def test_finish_reason_content_filter_raises_content_filtered(monkeypatch):
    """Per t-1 §16 + gateway/providers/openai.py:91-92."""
    v = _vendor()
    payload = SimpleNamespace(
        id="chatcmpl-cf",
        choices=[
            SimpleNamespace(
                finish_reason="content_filter",
                message=SimpleNamespace(content="filtered"),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=0, total_tokens=1),
    )
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(returns=payload)
    )
    with pytest.raises(ContentFiltered):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


async def test_message_content_none_returns_empty_text(monkeypatch):
    """Per t-1 §16 — `choice.message.content or ""` defends against None."""
    v = _vendor()
    payload = SimpleNamespace(
        id="chatcmpl-null",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=0, total_tokens=2),
    )
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(returns=payload)
    )
    r = await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)
    assert r.text == ""
    assert r.finish_reason == "stop"


async def test_usage_missing_defaults_to_zero(monkeypatch):
    """Per t-1 §16 — `getattr(usage, ..., 0) or 0` handles missing fields."""
    v = _vendor()
    payload = SimpleNamespace(
        id="chatcmpl-no-usage",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hi"),
            )
        ],
        usage=SimpleNamespace(),  # no prompt_tokens / completion_tokens attrs
    )
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(returns=payload)
    )
    r = await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)
    assert r.input_tokens == 0
    assert r.output_tokens == 0


async def test_apistatuserror_418_falls_through_to_bad_request(monkeypatch):
    """Per t-1 §16 — catch-all branch in gateway/providers/openai.py:87."""
    v = _vendor()
    exc = APIStatusError(message="teapot", response=_http_response(418), body=None)
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(BadRequest):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)
