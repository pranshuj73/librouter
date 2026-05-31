"""SecretsManager ABC plus env-backed and mock implementations.

`SecretsManager` is the single boundary for *outbound* vendor credentials.
Caller-API-key hashes live in Postgres and are never fetched via this layer.

In production, `EnvSecretsManager` reads from process env (mounted by the
container's secrets manager). In dev/tests, `MockSecretsManager` holds an
in-memory dict and is the default.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from gateway.models import SecretsMode


class SecretsManager(ABC):
    @abstractmethod
    def get(self, key: str) -> str:
        """Return the secret value. Raises KeyError if absent."""

    @abstractmethod
    def has(self, key: str) -> bool:
        """Return whether the secret is present. Must not raise."""


class EnvSecretsManager(SecretsManager):
    """Reads from `os.environ` at call time so late-bound env vars are seen.

    An env var set to the empty string is treated as *absent* — `${FOO:-}`
    style substitutions (notably docker-compose) materialize unset variables
    as empty strings, and an empty API key is never a usable secret.
    """

    def get(self, key: str) -> str:
        value = os.environ.get(key, "")
        if not value:
            raise KeyError(f"secret {key!r} not set in environment")
        return value

    def has(self, key: str) -> bool:
        return bool(os.environ.get(key, ""))


class MockSecretsManager(SecretsManager):
    """In-memory dict-backed manager. Seed via constructor or `.set()`."""

    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(seed) if seed else {}

    def get(self, key: str) -> str:
        try:
            return self._store[key]
        except KeyError as e:
            raise KeyError(f"secret {key!r} not present in MockSecretsManager") from e

    def has(self, key: str) -> bool:
        return key in self._store

    def set(self, key: str, value: str) -> None:
        self._store[key] = value


def build_secrets_manager(mode: SecretsMode) -> SecretsManager:
    if mode == "mock":
        return MockSecretsManager()
    if mode == "env":
        return EnvSecretsManager()
    raise ValueError(f"unknown secrets_mode: {mode!r}")
