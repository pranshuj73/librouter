"""Real OpenAI vendor adapter.

Wraps `openai.AsyncOpenAI`. Translates SDK errors to the `ProviderError`
taxonomy in `gateway.errors` and returns a normalized `ChatResult`.
"""

from __future__ import annotations

import asyncio

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
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


class OpenAIVendor(Vendor):
    name = "openai"

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(secrets)
        api_key = secrets.get("OPENAI_API_KEY")
        self._client = AsyncOpenAI(api_key=api_key)

    async def chat(
        self,
        model: str,
        messages: list[Message],
        params: ChatParams,
        timeout_s: float,
    ) -> ChatResult:
        kwargs: dict = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": params.max_tokens,
        }
        if params.temperature is not None:
            kwargs["temperature"] = params.temperature
        if params.top_p is not None:
            kwargs["top_p"] = params.top_p

        try:
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(**kwargs, timeout=timeout_s),
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

        choice = resp.choices[0]
        finish = choice.finish_reason
        if finish == "content_filter":
            raise ContentFiltered("openai content_filter")
        text = choice.message.content or ""
        usage = resp.usage
        return ChatResult(
            text=text,
            finish_reason=finish,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            vendor_request_id=getattr(resp, "id", None),
        )
