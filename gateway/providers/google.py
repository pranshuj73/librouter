"""Real Google Gemini vendor adapter.

The `google-genai` SDK is structured differently from OpenAI/Anthropic: an
explicit `Client` is constructed, and the `aio` namespace exposes async
methods. Generation parameters live in a `GenerateContentConfig`.

Security note (#4.2): raw SDK exception strings are stored in
``vendor_detail`` for operator logs only — never forwarded to callers.
"""

from __future__ import annotations

import asyncio

from google import genai
from google.genai import types as genai_types

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


def _convert_messages(messages: list[Message]) -> tuple[str | None, list[dict]]:
    system_parts: list[str] = []
    contents: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        role = "user" if m.role == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m.content}]})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, contents


class GoogleVendor(Vendor):
    name = "google"

    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(secrets)
        api_key = secrets.get("GOOGLE_API_KEY")
        self._client = genai.Client(api_key=api_key)

    async def chat(
        self,
        model: str,
        messages: list[Message],
        params: ChatParams,
        timeout_s: float,
    ) -> ChatResult:
        system, contents = _convert_messages(messages)

        cfg_kwargs: dict = {"max_output_tokens": params.max_tokens}
        if params.temperature is not None:
            cfg_kwargs["temperature"] = params.temperature
        if params.top_p is not None:
            cfg_kwargs["top_p"] = params.top_p
        if system is not None:
            cfg_kwargs["system_instruction"] = system
        config = genai_types.GenerateContentConfig(**cfg_kwargs)

        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as e:
            raise Timeout(type(e).__name__, vendor_detail=str(e)) from e
        except genai.errors.APIError as e:
            # google-genai surfaces an APIError with `.code` HTTP status
            status = getattr(e, "code", None) or getattr(e, "status_code", None) or 0
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = 0
            if status == 429:
                raise RateLimited(type(e).__name__, vendor_detail=str(e)) from e
            if status in (401, 403):
                raise AuthError(type(e).__name__, vendor_detail=str(e)) from e
            if status >= 500:
                raise Transient5xx(type(e).__name__, vendor_detail=str(e)) from e
            raise BadRequest(type(e).__name__, vendor_detail=str(e)) from e
        except Exception as e:  # pragma: no cover - defensive
            raise Transient5xx(type(e).__name__, vendor_detail=str(e)) from e

        # Extract text + usage.
        text = getattr(resp, "text", None)
        if text is None:
            parts: list[str] = []
            for cand in getattr(resp, "candidates", []) or []:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", []) or []:
                    t = getattr(part, "text", None)
                    if t:
                        parts.append(t)
            text = "".join(parts)

        finish_reason = None
        candidates = getattr(resp, "candidates", []) or []
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
            if finish_reason is not None:
                finish_reason = str(finish_reason)
            if finish_reason and "SAFETY" in finish_reason.upper():
                raise ContentFiltered(f"google safety: {finish_reason}")

        usage = getattr(resp, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)

        return ChatResult(
            text=text or "",
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            vendor_request_id=getattr(resp, "response_id", None),
        )
