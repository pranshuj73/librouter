"""Vendor factory.

`build_vendors(config, secrets)` returns a {provider_name: Vendor} dict.
* `provider_mode: mock` — the dict always contains all three mock vendors.
* `provider_mode: real` — each real vendor is constructed only if its API
  key is present in the secrets manager. A missing key is treated as
  "vendor disabled" rather than a fatal boot error, so an operator can run
  the gateway with just the providers they have keys for. The set of
  available providers is also fed to the routing refresh so candidates
  pointing at disabled providers automatically get weight 0.
"""

from __future__ import annotations

import logging

from gateway.models import Config
from gateway.providers.base import Vendor
from gateway.providers.mock import (
    MockAnthropicVendor,
    MockGoogleVendor,
    MockOpenAIVendor,
)
from gateway.secrets import SecretsManager


log = logging.getLogger(__name__)


# Key name each real vendor needs in the SecretsManager.
REAL_VENDOR_KEY_NAMES: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def build_vendors(cfg: Config, secrets: SecretsManager) -> dict[str, Vendor]:
    if cfg.provider_mode == "mock":
        return {
            "openai": MockOpenAIVendor(secrets),
            "anthropic": MockAnthropicVendor(secrets),
            "google": MockGoogleVendor(secrets),
        }

    from gateway.providers.anthropic import AnthropicVendor
    from gateway.providers.google import GoogleVendor
    from gateway.providers.openai import OpenAIVendor

    builders: dict[str, type[Vendor]] = {
        "openai": OpenAIVendor,
        "anthropic": AnthropicVendor,
        "google": GoogleVendor,
    }

    out: dict[str, Vendor] = {}
    for name, builder in builders.items():
        key_name = REAL_VENDOR_KEY_NAMES[name]
        if not secrets.has(key_name):
            log.warning(
                "skipping %s vendor: %s not set in secrets manager", name, key_name
            )
            continue
        try:
            out[name] = builder(secrets)
        except Exception:
            log.exception("failed to construct %s vendor; skipping", name)
    if not out:
        raise RuntimeError(
            "no real vendors could be constructed; set at least one of "
            + ", ".join(REAL_VENDOR_KEY_NAMES.values())
        )
    return out
