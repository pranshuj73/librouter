"""Shared mock vendor implementation.

Mock vendors are programmable: callers can queue a script of either a
successful `ChatResult` template or a `ProviderError` instance, plus an
optional `latency_s` to simulate slow calls (which also exercises the
`timeout_s` path).

The vendor name and a deterministic `vendor_request_id` prefix come from the
concrete subclass.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from gateway.errors import ProviderError, Timeout
from gateway.models import ChatParams, ChatResult, Message
from gateway.providers.base import Vendor


@dataclass(slots=True)
class _ScriptedResponse:
    """One step of a vendor's scripted behavior.

    Exactly one of `result` and `error` is set. `latency_s` is applied first
    via `asyncio.sleep`, then either the result is returned or the error is
    raised. If the simulated latency exceeds `timeout_s`, `Timeout` is raised
    regardless of which field was set.
    """

    result: ChatResult | None = None
    error: ProviderError | None = None
    latency_s: float = 0.0


class _MockVendorBase(Vendor):
    name = "abstract-mock"
    _vrid_prefix = "vrid-mock"

    def __init__(self, secrets=None, *, default_text: str = "ok") -> None:  # type: ignore[no-untyped-def]
        super().__init__(secrets=secrets)  # type: ignore[arg-type]
        self._script: deque[_ScriptedResponse] = deque()
        self._default_text = default_text
        self._call_count = 0

    # ---------------------------------------------------------------- scripting

    def queue(self, *steps: _ScriptedResponse) -> None:
        self._script.extend(steps)

    def queue_success(
        self,
        *,
        text: str | None = None,
        input_tokens: int = 10,
        output_tokens: int = 20,
        latency_s: float = 0.0,
        vendor_request_id: str | None = None,
        finish_reason: str = "stop",
    ) -> None:
        self._script.append(
            _ScriptedResponse(
                result=ChatResult(
                    text=text or self._default_text,
                    finish_reason=finish_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    vendor_request_id=vendor_request_id,
                ),
                latency_s=latency_s,
            )
        )

    def queue_error(self, exc: ProviderError, *, latency_s: float = 0.0) -> None:
        self._script.append(_ScriptedResponse(error=exc, latency_s=latency_s))

    def queue_each(self, steps: Iterable[_ScriptedResponse]) -> None:
        self._script.extend(steps)

    def clear(self) -> None:
        self._script.clear()
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    # ---------------------------------------------------------------- chat

    async def chat(
        self,
        model: str,
        messages: list[Message],
        params: ChatParams,
        timeout_s: float,
    ) -> ChatResult:
        self._call_count += 1
        step = self._next_step(model, messages)

        if step.latency_s > 0:
            if step.latency_s >= timeout_s:
                # Sleep for the timeout, then raise — preserves "I waited" behaviour.
                await asyncio.sleep(min(timeout_s, step.latency_s))
                raise Timeout(f"{self.name}: simulated timeout after {timeout_s:.2f}s")
            await asyncio.sleep(step.latency_s)

        if step.error is not None:
            raise step.error

        result = step.result
        assert result is not None
        if result.vendor_request_id is None:
            digest = hashlib.sha256(
                f"{model}|{''.join(m.content for m in messages)}".encode()
            ).hexdigest()[:12]
            result = result.model_copy(
                update={"vendor_request_id": f"{self._vrid_prefix}-{digest}"}
            )
        return result

    def _next_step(self, model: str, messages: list[Message]) -> _ScriptedResponse:
        if self._script:
            return self._script.popleft()
        # Default: deterministic success
        return _ScriptedResponse(
            result=ChatResult(
                text=self._default_text,
                finish_reason="stop",
                input_tokens=sum(len(m.content) for m in messages) // 4 + 1,
                output_tokens=len(self._default_text) // 4 + 1,
                vendor_request_id=None,
            )
        )
