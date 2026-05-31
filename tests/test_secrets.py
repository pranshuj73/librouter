"""Tests for gateway/secrets.py — SecretsManager ABC and the two impls.

TDD step 2. The interface must:
- Raise KeyError on miss (not return None, not raise something else)
- has() returns bool without raising
- MockSecretsManager.set() mutates in place
- EnvSecretsManager reads from os.environ at call time (not at construct time)
"""

from __future__ import annotations

import pytest

from gateway.secrets import (
    EnvSecretsManager,
    MockSecretsManager,
    SecretsManager,
    build_secrets_manager,
)


def test_mock_seed_and_get():
    m = MockSecretsManager({"OPENAI_API_KEY": "sk-xyz"})
    assert m.get("OPENAI_API_KEY") == "sk-xyz"
    assert m.has("OPENAI_API_KEY") is True


def test_mock_missing_raises_keyerror():
    m = MockSecretsManager()
    with pytest.raises(KeyError):
        m.get("NOPE")
    assert m.has("NOPE") is False


def test_mock_set_updates():
    m = MockSecretsManager()
    m.set("ANTHROPIC_API_KEY", "ant-1")
    assert m.get("ANTHROPIC_API_KEY") == "ant-1"
    m.set("ANTHROPIC_API_KEY", "ant-2")
    assert m.get("ANTHROPIC_API_KEY") == "ant-2"


def test_env_reads_from_environ(monkeypatch):
    monkeypatch.setenv("FOO_KEY", "foo-value")
    m = EnvSecretsManager()
    assert m.get("FOO_KEY") == "foo-value"
    assert m.has("FOO_KEY") is True


def test_env_reads_at_call_time(monkeypatch):
    m = EnvSecretsManager()
    monkeypatch.delenv("DYNAMIC_KEY", raising=False)
    assert m.has("DYNAMIC_KEY") is False
    monkeypatch.setenv("DYNAMIC_KEY", "set-after-construct")
    assert m.get("DYNAMIC_KEY") == "set-after-construct"


def test_env_missing_raises_keyerror(monkeypatch):
    monkeypatch.delenv("ABSOLUTELY_MISSING", raising=False)
    m = EnvSecretsManager()
    with pytest.raises(KeyError):
        m.get("ABSOLUTELY_MISSING")


def test_both_are_secretsmanager_instances():
    assert isinstance(MockSecretsManager(), SecretsManager)
    assert isinstance(EnvSecretsManager(), SecretsManager)


def test_build_factory_mock():
    m = build_secrets_manager("mock")
    assert isinstance(m, MockSecretsManager)


def test_build_factory_env():
    m = build_secrets_manager("env")
    assert isinstance(m, EnvSecretsManager)


def test_build_factory_invalid_mode():
    with pytest.raises(ValueError):
        build_secrets_manager("bogus")  # type: ignore[arg-type]
