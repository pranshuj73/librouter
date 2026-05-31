"""Pricing lookup table backed by the vendored LiteLLM JSON file.

Usage::

    from gateway.pricing import load_pricing

    table = load_pricing()
    cost = table.cost_usd(provider="openai", model="gpt-4o-mini",
                          input_tokens=1000, output_tokens=500)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

_DEFAULT_JSON = Path(__file__).parent / "data" / "model_prices_and_context_window.json"

# Maps our internal provider names to litellm_provider values in the JSON.
# A provider may map to multiple litellm values (tuple); first match wins.
_PROVIDER_LITELLM_MAP: dict[str, tuple[str, ...]] = {
    "openai": ("openai",),
    "anthropic": ("anthropic",),
    "google": ("vertex_ai-language-models", "vertex_ai"),
}


class _Entry(NamedTuple):
    input_cost_per_token: float
    output_cost_per_token: float
    max_input_tokens: int | None


class PricingTable:
    """O(1) lookup table built from the LiteLLM pricing JSON."""

    def __init__(self, index: dict[tuple[str, str], _Entry]) -> None:
        self._index = index
        self._warned: set[tuple[str, str]] = set()

    # ---------------------------------------------------------------- public

    def has(self, *, provider: str, model: str) -> bool:
        """Return True iff (provider, model) has a known price."""
        return (provider, model) in self._index

    def cost_usd(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Return total cost in USD for the given token counts.

        Returns 0.0 for unknown (provider, model) pairs and logs a warning
        once per unknown pair.
        """
        entry = self._index.get((provider, model))
        if entry is None:
            key = (provider, model)
            if key not in self._warned:
                self._warned.add(key)
                log.warning(
                    "pricing: no price entry for provider=%r model=%r; cost=0.0",
                    provider,
                    model,
                )
            return 0.0
        return (
            input_tokens * entry.input_cost_per_token
            + output_tokens * entry.output_cost_per_token
        )

    def context_window(self, *, provider: str, model: str) -> int | None:
        """Return max_input_tokens for (provider, model), or None if unknown."""
        entry = self._index.get((provider, model))
        if entry is None:
            return None
        return entry.max_input_tokens


def _build_index(raw: dict) -> dict[tuple[str, str], _Entry]:
    """Build a (provider, model) -> _Entry index from the raw JSON dict."""
    # Build reverse map: litellm_provider -> our provider name(s)
    litellm_to_ours: dict[str, str] = {}
    for our_provider, litellm_providers in _PROVIDER_LITELLM_MAP.items():
        for lp in litellm_providers:
            # First mapping wins if there's a collision (unlikely)
            litellm_to_ours.setdefault(lp, our_provider)

    index: dict[tuple[str, str], _Entry] = {}

    for model_key, info in raw.items():
        if model_key == "sample_spec":
            continue
        if not isinstance(info, dict):
            continue

        litellm_provider = info.get("litellm_provider", "")
        our_provider = litellm_to_ours.get(litellm_provider)
        if our_provider is None:
            continue

        input_cost = info.get("input_cost_per_token")
        output_cost = info.get("output_cost_per_token")
        if input_cost is None or output_cost is None:
            continue

        max_input = info.get("max_input_tokens")
        # Tolerate string values in the sample_spec documentation rows
        if isinstance(max_input, str):
            max_input = None

        entry = _Entry(
            input_cost_per_token=float(input_cost),
            output_cost_per_token=float(output_cost),
            max_input_tokens=int(max_input) if max_input is not None else None,
        )
        index[(our_provider, model_key)] = entry

    return index


def load_pricing(json_path: Path | None = None) -> PricingTable:
    """Load the pricing table from *json_path* (defaults to the vendored file).

    The returned :class:`PricingTable` pre-builds an index so all lookups
    are O(1).
    """
    path = json_path if json_path is not None else _DEFAULT_JSON
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    index = _build_index(raw)
    return PricingTable(index)
