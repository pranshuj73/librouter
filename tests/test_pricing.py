"""Tests for gateway/pricing.py.

TDD sequence — one failing test written before every implementing line.
"""

from __future__ import annotations

import json
import logging
import math
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Step 1 — import the module and load the default file
# ---------------------------------------------------------------------------


def test_load_pricing_imports_and_returns_table():
    """Fails until gateway/pricing.py exists with load_pricing()."""
    from gateway.pricing import PricingTable, load_pricing

    table = load_pricing()
    assert isinstance(table, PricingTable)


# ---------------------------------------------------------------------------
# Step 2 — has() checks for known and unknown pairs
# ---------------------------------------------------------------------------


def test_has_known_and_unknown_pairs():
    """has() returns True for a real model and False for nonsense."""
    from gateway.pricing import load_pricing

    table = load_pricing()
    assert table.has(provider="openai", model="gpt-4o-mini") is True
    assert table.has(provider="openai", model="nonexistent-xyz-9999") is False
    assert table.has(provider="fakeprovider", model="gpt-4o-mini") is False


# ---------------------------------------------------------------------------
# Step 3 — cost_usd for openai/gpt-4o-mini
# ---------------------------------------------------------------------------


def test_cost_usd_openai_gpt4o_mini():
    """cost_usd uses per-token rates directly (no /1_000_000 division)."""
    from gateway.pricing import load_pricing

    table = load_pricing()
    # From the JSON: input=1.5e-7, output=6e-7
    expected = 1000 * 0.00000015 + 500 * 0.0000006
    result = table.cost_usd(
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=1000,
        output_tokens=500,
    )
    assert math.isclose(result, expected, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Step 6 — unknown pair returns 0.0 and does not raise
# ---------------------------------------------------------------------------


def test_cost_usd_unknown_pair_returns_zero():
    """An unrecognised (provider, model) must return 0.0 without raising."""
    from gateway.pricing import load_pricing

    table = load_pricing()
    cost = table.cost_usd(
        provider="openai",
        model="totally-made-up-model",
        input_tokens=1000,
        output_tokens=500,
    )
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Step 7 — unknown pair logs a warning once, not twice
# ---------------------------------------------------------------------------


def test_cost_usd_unknown_pair_warns_once(caplog):
    """Warning is emitted on first call for an unknown pair; suppressed on repeat."""
    from gateway.pricing import load_pricing

    table = load_pricing()
    pair_kwargs = dict(
        provider="openai",
        model="warn-dedup-test-model-xyz",
        input_tokens=1,
        output_tokens=1,
    )

    with caplog.at_level(logging.WARNING, logger="gateway.pricing"):
        table.cost_usd(**pair_kwargs)
    first_warnings = [r for r in caplog.records if "warn-dedup-test-model-xyz" in r.message]
    assert len(first_warnings) == 1, "Expected exactly one warning on first call"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="gateway.pricing"):
        table.cost_usd(**pair_kwargs)
    second_warnings = [r for r in caplog.records if "warn-dedup-test-model-xyz" in r.message]
    assert len(second_warnings) == 0, "Expected no warning on second call for same pair"


# ---------------------------------------------------------------------------
# Step 8 — context_window() for openai/gpt-4o-mini
# ---------------------------------------------------------------------------


def test_context_window_known_model():
    """context_window returns max_input_tokens=128000 for gpt-4o-mini."""
    from gateway.pricing import load_pricing

    table = load_pricing()
    cw = table.context_window(provider="openai", model="gpt-4o-mini")
    assert cw == 128000


# ---------------------------------------------------------------------------
# Step 9 — context_window() returns None for unknown pair
# ---------------------------------------------------------------------------


def test_context_window_unknown_pair_returns_none():
    """context_window returns None gracefully for an unrecognised pair."""
    from gateway.pricing import load_pricing

    table = load_pricing()
    cw = table.context_window(provider="openai", model="nonexistent-xyz-9999")
    assert cw is None


# ---------------------------------------------------------------------------
# Step 10 — load_pricing with explicit json_path reads a tiny temp file
# ---------------------------------------------------------------------------


def test_load_pricing_with_explicit_path(tmp_path):
    """Passing a custom json_path loads that file's entries."""
    from gateway.pricing import load_pricing

    mini_data = {
        "my-test-model": {
            "litellm_provider": "openai",
            "input_cost_per_token": 0.001,
            "output_cost_per_token": 0.002,
            "max_input_tokens": 4096,
        },
        "google-test-model": {
            "litellm_provider": "vertex_ai-language-models",
            "input_cost_per_token": 0.0005,
            "output_cost_per_token": 0.001,
            "max_input_tokens": 8192,
        },
    }
    json_file = tmp_path / "prices.json"
    json_file.write_text(json.dumps(mini_data), encoding="utf-8")

    table = load_pricing(json_path=json_file)

    assert table.has(provider="openai", model="my-test-model")
    assert table.has(provider="google", model="google-test-model")
    assert not table.has(provider="openai", model="gpt-4o-mini"), (
        "Custom path should not include the default file's entries"
    )

    cost = table.cost_usd(
        provider="openai", model="my-test-model", input_tokens=100, output_tokens=50
    )
    assert math.isclose(cost, 100 * 0.001 + 50 * 0.002, rel_tol=1e-9)
    assert table.context_window(provider="openai", model="my-test-model") == 4096


# ---------------------------------------------------------------------------
# Step 11 — sample_spec key is skipped
# ---------------------------------------------------------------------------


def test_sample_spec_is_not_indexed():
    """The documentation entry 'sample_spec' must never appear as a real model."""
    from gateway.pricing import load_pricing

    table = load_pricing()
    assert table.has(provider="openai", model="sample_spec") is False


# ---------------------------------------------------------------------------
# Step 5 — google/gemini-2.5-flash via vertex_ai-language-models normalization
# ---------------------------------------------------------------------------


def test_cost_usd_google_gemini_flash_normalization():
    """provider='google' maps to litellm vertex_ai-language-models entries.

    gemini-2.5-flash JSON: input=3e-7, output=2.5e-6.
    Any non-zero cost proves the normalization is wired correctly.
    """
    from gateway.pricing import load_pricing

    table = load_pricing()
    assert table.has(provider="google", model="gemini-2.5-flash"), (
        "gemini-2.5-flash must be indexed under provider='google'"
    )
    cost = table.cost_usd(
        provider="google",
        model="gemini-2.5-flash",
        input_tokens=1000,
        output_tokens=500,
    )
    assert cost > 0.0, "Expected non-zero cost via google/vertex_ai-language-models mapping"


# ---------------------------------------------------------------------------
# Step 4 — cost_usd for anthropic/claude-haiku-4-5
# ---------------------------------------------------------------------------


def test_cost_usd_anthropic_claude_haiku():
    """Confirms the table is not openai-specific.

    claude-haiku-4-5 JSON values: input=1e-6, output=5e-6.
    """
    from gateway.pricing import load_pricing

    table = load_pricing()
    expected = 200 * 1e-6 + 100 * 5e-6
    result = table.cost_usd(
        provider="anthropic",
        model="claude-haiku-4-5",
        input_tokens=200,
        output_tokens=100,
    )
    assert math.isclose(result, expected, rel_tol=1e-9)
