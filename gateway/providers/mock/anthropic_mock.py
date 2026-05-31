from __future__ import annotations

from gateway.providers.mock._base_mock import _MockVendorBase, _ScriptedResponse


class MockAnthropicVendor(_MockVendorBase):
    name = "anthropic"
    _vrid_prefix = "vrid-anthropic-mock"


__all__ = ["MockAnthropicVendor", "_ScriptedResponse"]
