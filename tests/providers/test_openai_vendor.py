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
    exc = AuthenticationError(message="nope", response=_http_response(401), body=None)
    monkeypatch.setattr(
        v._client.chat.completions, "create", _stub_create(raises=exc)
    )
    with pytest.raises(AuthError):
        await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)


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
