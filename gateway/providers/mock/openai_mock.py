from __future__ import annotations

from gateway.providers.mock._base_mock import _MockVendorBase, _ScriptedResponse


class MockOpenAIVendor(_MockVendorBase):
    name = "openai"
    _vrid_prefix = "vrid-openai-mock"


__all__ = ["MockOpenAIVendor", "_ScriptedResponse"]
