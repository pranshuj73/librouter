"""Real Anthropic vendor adapter."""

from __future__ import annotations

import asyncio

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
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
from gateway.models import ChatParams, ChatResult, Message
from gateway.providers.base import Vendor
from gateway.secrets import SecretsManager


def _split_system(messages: list[Message]) -> tuple[str | None, list[dict]]:
    """Anthropic wants `system` as a top-level field, not a message."""
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        rest.append({"role": m.role, "content": m.content})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, rest


class AnthropicVendor(Vendor):
    name = "anthropic"

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(secrets)
        api_key = secrets.get("ANTHROPIC_API_KEY")
        self._client = AsyncAnthropic(api_key=api_key)

    async def chat(
        self,
        model: str,
        messages: list[Message],
        params: ChatParams,
        timeout_s: float,
    ) -> ChatResult:
        system, conv = _split_system(messages)
        kwargs: dict = {
            "model": model,
            "max_tokens": params.max_tokens,
            "messages": conv,
        }
        if system is not None:
            kwargs["system"] = system
        if params.temperature is not None:
            kwargs["temperature"] = params.temperature
        if params.top_p is not None:
            kwargs["top_p"] = params.top_p

        try:
            resp = await asyncio.wait_for(
                self._client.messages.create(**kwargs, timeout=timeout_s),
                timeout=timeout_s + 0.5,
            )
        except asyncio.TimeoutError as e:
            raise Timeout(str(e)) from e
        except APITimeoutError as e:
            raise Timeout(str(e)) from e
        except RateLimitError as e:
            raise RateLimited(str(e)) from e
        except AuthenticationError as e:
            raise AuthError(str(e)) from e
        except BadRequestError as e:
            raise BadRequest(str(e)) from e
        except InternalServerError as e:
            raise Transient5xx(str(e)) from e
        except APIConnectionError as e:
            raise Transient5xx(str(e)) from e
        except APIStatusError as e:
            if 500 <= e.status_code < 600:
                raise Transient5xx(str(e)) from e
            if e.status_code == 429:
                raise RateLimited(str(e)) from e
            raise BadRequest(str(e)) from e

        # Anthropic returns a list of content blocks; we concatenate text blocks.
        text_parts: list[str] = []
        for block in resp.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts)

        stop_reason = resp.stop_reason
        if stop_reason == "refusal":
            raise ContentFiltered("anthropic refusal")

        usage = resp.usage
        return ChatResult(
            text=text,
            finish_reason=stop_reason,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            vendor_request_id=getattr(resp, "id", None),
        )
