"""Tests for gateway/providers.build_vendors.

Covers the partial-vendor case: in real mode, only providers whose API key
is present in the secrets manager get a constructed vendor. Tiers may still
reference all three; the refresh task is responsible for filtering candidates
to the available_providers set.
"""

from __future__ import annotations

import pytest

from gateway.models import Config
from gateway.providers import build_vendors
from gateway.providers.base import Vendor
from gateway.secrets import MockSecretsManager


def _cfg(mode: str) -> Config:
    return Config.model_validate(
        {
            "provider_mode": mode,
            "secrets_mode": "mock",
            "tiers": {
                "fast": [
                    {"provider": "openai", "model": "gpt-mini", "weight": 50},
                    {"provider": "anthropic", "model": "haiku", "weight": 30},
                    {"provider": "google", "model": "flash", "weight": 20},
                ],
            },
            "routing": {},
            "prices": {
                "openai/gpt-mini": {"input": 0.15, "output": 0.6},
                "anthropic/haiku": {"input": 1.0, "output": 5.0},
                "google/flash": {"input": 0.3, "output": 2.5},
            },
            "rate_limits": {
                "openai/gpt-mini": {"rpm": 100, "tpm": 10000},
                "anthropic/haiku": {"rpm": 100, "tpm": 10000},
                "google/flash": {"rpm": 100, "tpm": 10000},
            },
            "callers": [{"name": "t", "key_hash": "sha256:x"}],
        }
    )


def test_mock_mode_always_returns_all_three():
    vendors = build_vendors(_cfg("mock"), MockSecretsManager())
    assert set(vendors.keys()) == {"openai", "anthropic", "google"}
    for v in vendors.values():
        assert isinstance(v, Vendor)


def test_real_mode_partial_keys_only_builds_present_vendors():
    secrets = MockSecretsManager(
        {"OPENAI_API_KEY": "sk-x", "GOOGLE_API_KEY": "g-y"}
    )
    vendors = build_vendors(_cfg("real"), secrets)
    assert set(vendors.keys()) == {"openai", "google"}
    assert "anthropic" not in vendors


def test_real_mode_no_keys_raises():
    with pytest.raises(RuntimeError):
        build_vendors(_cfg("real"), MockSecretsManager())


def test_real_mode_single_key_builds_one():
    secrets = MockSecretsManager({"OPENAI_API_KEY": "sk-x"})
    vendors = build_vendors(_cfg("real"), secrets)
    assert set(vendors.keys()) == {"openai"}


# --------------------------------------------------------------------- t-1 §19
# Additions per docs/code-review/t-1.md §19.


def test_vendor_constructor_failure_is_skipped(monkeypatch):
    """Per t-1 §19 — `except Exception: log.exception` branch at
    gateway/providers/__init__.py:66-67.

    Provide only OpenAI and Anthropic keys (so Google is naturally skipped),
    then break Anthropic's constructor and assert build_vendors returns just
    OpenAI without raising.
    """
    # Import here so the monkeypatch targets the real (importable) class.
    from gateway.providers import anthropic as anthropic_mod

    def _bad_init(self, secrets):  # noqa: ARG001
        raise RuntimeError("nope")

    monkeypatch.setattr(anthropic_mod.AnthropicVendor, "__init__", _bad_init)

    secrets = MockSecretsManager(
        {"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "ant-y"}
    )
    vendors = build_vendors(_cfg("real"), secrets)
    assert set(vendors.keys()) == {"openai"}
    assert "anthropic" not in vendors


def test_mock_mode_ignores_real_keys():
    """Per t-1 §19 — `provider_mode="mock"` returns mock vendors regardless of
    real-mode keys present in the secrets manager.
    """
    from gateway.providers.mock import (
        MockAnthropicVendor,
        MockGoogleVendor,
        MockOpenAIVendor,
    )

    secrets = MockSecretsManager(
        {
            "OPENAI_API_KEY": "sk-x",
            "ANTHROPIC_API_KEY": "ant-y",
            "GOOGLE_API_KEY": "g-z",
        }
    )
    vendors = build_vendors(_cfg("mock"), secrets)
    assert isinstance(vendors["openai"], MockOpenAIVendor)
    assert isinstance(vendors["anthropic"], MockAnthropicVendor)
    assert isinstance(vendors["google"], MockGoogleVendor)
