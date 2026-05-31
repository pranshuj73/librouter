"""`Vendor` abstract base class.

All vendors — real and mock — implement this. The router talks only to
`Vendor` instances and never sees vendor-specific SDK shapes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from gateway.models import ChatParams, ChatResult, Message
from gateway.secrets import SecretsManager


class Vendor(ABC):
    """One concrete provider (openai, anthropic, google).

    `name` matches the value used as `provider:` in `config.yaml` and tier
    candidate entries.
    """

    name: str = "abstract"

    def __init__(self, secrets: SecretsManager) -> None:
        self._secrets = secrets

    @abstractmethod
    async def chat(
        self,
        model: str,
        messages: list[Message],
        params: ChatParams,
        timeout_s: float,
    ) -> ChatResult:
        """Return a normalized `ChatResult` or raise a `ProviderError`."""
