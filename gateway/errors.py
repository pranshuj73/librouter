"""Provider error taxonomy + helpers to map them to caller-visible responses.

`ProviderError` (and its subclasses) are the *only* exception types the router
will catch from a `Vendor` adapter. Adapters must translate vendor-specific
errors into one of these — anything else propagates as a 500 to the caller.
"""

from __future__ import annotations

from gateway.models import ErrorBody, ProviderErrorKind


class ProviderError(Exception):
    """Base class for normalized vendor errors."""

    kind: ProviderErrorKind = ProviderErrorKind.TRANSIENT_5XX

    def __init__(self, message: str = "") -> None:
        super().__init__(message or type(self).__name__)


class RateLimited(ProviderError):
    kind = ProviderErrorKind.RATE_LIMITED


class Transient5xx(ProviderError):
    kind = ProviderErrorKind.TRANSIENT_5XX


class Timeout(ProviderError):
    kind = ProviderErrorKind.TIMEOUT


class BadRequest(ProviderError):
    kind = ProviderErrorKind.BAD_REQUEST


class AuthError(ProviderError):
    kind = ProviderErrorKind.AUTH


class ContentFiltered(ProviderError):
    kind = ProviderErrorKind.CONTENT_FILTERED


RETRYABLE_ERROR_TYPES: tuple[type[ProviderError], ...] = (
    RateLimited,
    Transient5xx,
    Timeout,
)

NON_RETRYABLE_ERROR_TYPES: tuple[type[ProviderError], ...] = (
    BadRequest,
    AuthError,
    ContentFiltered,
)


def caller_error_for(exc: ProviderError) -> tuple[int, ErrorBody]:
    """HTTP status + body for caller errors that should NOT be failed over."""
    if isinstance(exc, BadRequest):
        return 400, ErrorBody(type="invalid_request", message=str(exc), retryable=False)
    if isinstance(exc, AuthError):
        return 401, ErrorBody(type="auth", message=str(exc), retryable=False)
    if isinstance(exc, ContentFiltered):
        return 400, ErrorBody(type="content_filtered", message=str(exc), retryable=False)
    # Should not happen — the router filters retryables.
    return 500, ErrorBody(type="internal", message=str(exc), retryable=False)
