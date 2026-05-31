from __future__ import annotations

from gateway.providers.mock._base_mock import _MockVendorBase, _ScriptedResponse


class MockGoogleVendor(_MockVendorBase):
    name = "google"
    _vrid_prefix = "vrid-google-mock"


__all__ = ["MockGoogleVendor", "_ScriptedResponse"]
