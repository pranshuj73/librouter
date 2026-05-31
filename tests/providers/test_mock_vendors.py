"""Tests for the mock Vendor implementations.

TDD step 9. The mocks are programmable enough to drive every router/breaker/
ratelimit code path without an HTTP layer.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from gateway.errors import BadRequest, RateLimited, Transient5xx
from gateway.models import ChatParams, Message
from gateway.providers.base import Vendor
from gateway.providers.mock import (
    MockAnthropicVendor,
    MockGoogleVendor,
    MockOpenAIVendor,
)
from gateway.secrets import MockSecretsManager


pytestmark = pytest.mark.asyncio


def _msg() -> list[Message]:
    return [Message(role="user", content="hello")]


def _params() -> ChatParams:
    return ChatParams(max_tokens=64)


async def test_all_mocks_are_vendor_instances():
    s = MockSecretsManager()
    assert isinstance(MockOpenAIVendor(s), Vendor)
    assert isinstance(MockAnthropicVendor(s), Vendor)
    assert isinstance(MockGoogleVendor(s), Vendor)


async def test_default_success_response():
    v = MockOpenAIVendor(MockSecretsManager())
    r = await v.chat("gpt-4o-mini", _msg(), _params(), timeout_s=5.0)
    assert r.text == "ok"
    assert r.input_tokens > 0
    assert r.output_tokens > 0
    assert r.vendor_request_id is not None
    assert r.vendor_request_id.startswith("vrid-openai-mock-")


async def test_queue_success_with_explicit_tokens():
    v = MockAnthropicVendor(MockSecretsManager())
    v.queue_success(text="hi there", input_tokens=42, output_tokens=11)
    r = await v.chat("haiku", _msg(), _params(), timeout_s=5.0)
    assert r.text == "hi there"
    assert r.input_tokens == 42
    assert r.output_tokens == 11


async def test_scripted_error_sequence_failover_pattern():
    v = MockOpenAIVendor(MockSecretsManager())
    v.queue_error(RateLimited("oops"))
    v.queue_error(Transient5xx("nope"))
    v.queue_success(text="finally")
    with pytest.raises(RateLimited):
        await v.chat("gpt-4o", _msg(), _params(), timeout_s=5.0)
    with pytest.raises(Transient5xx):
        await v.chat("gpt-4o", _msg(), _params(), timeout_s=5.0)
    r = await v.chat("gpt-4o", _msg(), _params(), timeout_s=5.0)
    assert r.text == "finally"


async def test_simulated_latency_respected():
    v = MockGoogleVendor(MockSecretsManager())
    v.queue_success(latency_s=0.05)
    t0 = time.monotonic()
    await v.chat("gemini", _msg(), _params(), timeout_s=5.0)
    assert time.monotonic() - t0 >= 0.04


async def test_simulated_latency_above_timeout_raises_timeout():
    from gateway.errors import Timeout
    v = MockOpenAIVendor(MockSecretsManager())
    v.queue_success(latency_s=1.0)
    with pytest.raises(Timeout):
        await v.chat("gpt-4o", _msg(), _params(), timeout_s=0.05)


async def test_bad_request_does_not_get_retried_by_vendor():
    v = MockOpenAIVendor(MockSecretsManager())
    v.queue_error(BadRequest("bad messages"))
    with pytest.raises(BadRequest):
        await v.chat("gpt-4o", _msg(), _params(), timeout_s=5.0)


async def test_call_count_tracks_attempts():
    v = MockAnthropicVendor(MockSecretsManager())
    for _ in range(3):
        await v.chat("haiku", _msg(), _params(), timeout_s=5.0)
    assert v.call_count == 3


async def test_deterministic_vendor_request_id():
    v1 = MockOpenAIVendor(MockSecretsManager())
    v2 = MockOpenAIVendor(MockSecretsManager())
    r1 = await v1.chat("gpt-4o", _msg(), _params(), timeout_s=5.0)
    r2 = await v2.chat("gpt-4o", _msg(), _params(), timeout_s=5.0)
    assert r1.vendor_request_id == r2.vendor_request_id


async def test_explicit_vendor_request_id_preserved():
    v = MockOpenAIVendor(MockSecretsManager())
    v.queue_success(vendor_request_id="custom-id-1")
    r = await v.chat("gpt-4o", _msg(), _params(), timeout_s=5.0)
    assert r.vendor_request_id == "custom-id-1"
