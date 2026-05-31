"""Contract test for the real Anthropic adapter — SDK-level monkeypatching."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from anthropic import (
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
from gateway.providers.anthropic import AnthropicVendor
from gateway.providers.base import Vendor
from gateway.secrets import MockSecretsManager


pytestmark = pytest.mark.asyncio


def _vendor() -> AnthropicVendor:
    return AnthropicVendor(MockSecretsManager({"ANTHROPIC_API_KEY": "ant-test"}))


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


def _http_response(status: int) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        content=b'{"error":{"message":"x"}}',
    )


def _stub_success_payload() -> SimpleNamespace:
    return SimpleNamespace(
        id="msg_abc",
        content=[SimpleNamespace(type="text", text="hi there")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=8, output_tokens=4),
    )


async def test_is_vendor_instance():
    assert isinstance(_vendor(), Vendor)


async def test_success_normalizes_to_chat_result(monkeypatch):
    v = _vendor()
    monkeypatch.setattr(
        v._client.messages, "create", _stub_create(returns=_stub_success_payload())
    )
    r = await v.chat("haiku", _msg(), _params(), timeout_s=5.0)
    assert r.text == "hi there"
    assert r.input_tokens == 8
    assert r.output_tokens == 4
    assert r.vendor_request_id == "msg_abc"
    assert r.finish_reason == "end_turn"


async def test_429_maps_to_rate_limited(monkeypatch):
    v = _vendor()
    exc = RateLimitError(message="rl", response=_http_response(429), body=None)
    monkeypatch.setattr(v._client.messages, "create", _stub_create(raises=exc))
    with pytest.raises(RateLimited):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)


async def test_500_maps_to_transient5xx(monkeypatch):
    v = _vendor()
    exc = InternalServerError(message="boom", response=_http_response(500), body=None)
    monkeypatch.setattr(v._client.messages, "create", _stub_create(raises=exc))
    with pytest.raises(Transient5xx):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)


async def test_401_maps_to_auth_error(monkeypatch):
    v = _vendor()
    exc = AuthenticationError(message="invalid x-api-key", response=_http_response(401), body=None)
    monkeypatch.setattr(v._client.messages, "create", _stub_create(raises=exc))
    with pytest.raises(AuthError) as exc_info:
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)
    # Vendor SDK message must NOT appear in the public ProviderError string (#4.2).
    assert "invalid x-api-key" not in str(exc_info.value)


async def test_400_maps_to_bad_request(monkeypatch):
    v = _vendor()
    exc = BadRequestError(message="bad", response=_http_response(400), body=None)
    monkeypatch.setattr(v._client.messages, "create", _stub_create(raises=exc))
    with pytest.raises(BadRequest):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)


async def test_timeout_maps_to_timeout(monkeypatch):
    v = _vendor()
    exc = APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    monkeypatch.setattr(v._client.messages, "create", _stub_create(raises=exc))
    with pytest.raises(Timeout):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)


async def test_connection_error_maps_to_transient5xx(monkeypatch):
    v = _vendor()
    exc = APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    monkeypatch.setattr(v._client.messages, "create", _stub_create(raises=exc))
    with pytest.raises(Transient5xx):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)


async def test_apistatuserror_503_maps_to_transient5xx(monkeypatch):
    v = _vendor()
    exc = APIStatusError(message="x", response=_http_response(503), body=None)
    monkeypatch.setattr(v._client.messages, "create", _stub_create(raises=exc))
    with pytest.raises(Transient5xx):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)


# --------------------------------------------------------------------- t-1 §17
# Additions per docs/code-review/t-1.md §17 — uncovered branches:
# `_split_system` joining, refusal mapping, mixed/empty content blocks, missing
# usage fields.


def _capture_kwargs_stub(returns):
    """Stub that records the kwargs it was called with into the closure."""
    captured: dict = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return returns

    return _create, captured


async def test_split_system_joins_multiple_system_messages(monkeypatch):
    """Per t-1 §17 — gateway/providers/anthropic.py:_split_system joins with `\\n\\n`."""
    v = _vendor()
    create, captured = _capture_kwargs_stub(_stub_success_payload())
    monkeypatch.setattr(v._client.messages, "create", create)

    messages = [
        Message(role="system", content="a"),
        Message(role="user", content="u"),
        Message(role="system", content="b"),
    ]
    await v.chat("haiku", messages, _params(), timeout_s=5.0)

    assert captured["system"] == "a\n\nb"
    # Non-system messages only — the user message survives, system was split out.
    assert captured["messages"] == [{"role": "user", "content": "u"}]


async def test_stop_reason_refusal_raises_content_filtered(monkeypatch):
    """Per t-1 §17 — gateway/providers/anthropic.py:111-112."""
    v = _vendor()
    payload = SimpleNamespace(
        id="msg_refuse",
        content=[SimpleNamespace(type="text", text="I cannot help with that.")],
        stop_reason="refusal",
        usage=SimpleNamespace(input_tokens=3, output_tokens=7),
    )
    monkeypatch.setattr(v._client.messages, "create", _stub_create(returns=payload))
    with pytest.raises(ContentFiltered):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)


async def test_content_with_non_text_blocks_only_concatenates_text(monkeypatch):
    """Per t-1 §17 — only `type == "text"` blocks are concatenated."""
    v = _vendor()
    payload = SimpleNamespace(
        id="msg_mixed",
        content=[
            SimpleNamespace(type="text", text="A"),
            SimpleNamespace(type="image", text=""),
            SimpleNamespace(type="text", text="B"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    monkeypatch.setattr(v._client.messages, "create", _stub_create(returns=payload))
    r = await v.chat("haiku", _msg(), _params(), timeout_s=5.0)
    assert r.text == "AB"


async def test_content_empty_list_returns_empty_text(monkeypatch):
    """Per t-1 §17 — `content=[]` → `text == ""`."""
    v = _vendor()
    payload = SimpleNamespace(
        id="msg_empty",
        content=[],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=0),
    )
    monkeypatch.setattr(v._client.messages, "create", _stub_create(returns=payload))
    r = await v.chat("haiku", _msg(), _params(), timeout_s=5.0)
    assert r.text == ""


async def test_usage_missing_defaults_to_zero(monkeypatch):
    """Per t-1 §17 — `getattr(usage, ..., 0) or 0` handles missing fields."""
    v = _vendor()
    payload = SimpleNamespace(
        id="msg_no_usage",
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(),  # no input_tokens / output_tokens attrs
    )
    monkeypatch.setattr(v._client.messages, "create", _stub_create(returns=payload))
    r = await v.chat("haiku", _msg(), _params(), timeout_s=5.0)
    assert r.input_tokens == 0
    assert r.output_tokens == 0
