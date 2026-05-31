"""Provider error taxonomy + helpers to map them to caller-visible responses.

`ProviderError` (and its subclasses) are the *only* exception types the router
will catch from a `Vendor` adapter. Adapters must translate vendor-specific
errors into one of these — anything else propagates as a 500 to the caller.

Security note (#4.2): ``caller_error_for`` produces fixed canonical strings in
the caller-visible response body.  Vendor SDK messages (which can contain
upstream response bodies, request IDs, or key fragments) are kept in
``ProviderError.vendor_detail`` for structured-log use only; they never reach
the caller.
"""

from __future__ import annotations

from gateway.models import ErrorBody, ProviderErrorKind


class ProviderError(Exception):
    """Base class for normalized vendor errors.

    Parameters
    ----------
    message:
        A short, safe public string (e.g. the exception class name).  This is
        what ``str(exc)`` returns and what appears in the caller-visible body
        when ``caller_error_for`` is called — it must not contain raw vendor
        SDK text.
    vendor_detail:
        The raw SDK error string, kept for operator logs only.  Never forwarded
        to callers.
    """

    kind: ProviderErrorKind = ProviderErrorKind.TRANSIENT_5XX

    def __init__(self, message: str = "", *, vendor_detail: str | None = None) -> None:
        super().__init__(message or type(self).__name__)
        self.vendor_detail: str | None = vendor_detail


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

# Canonical caller-visible messages — never include vendor SDK text.
_CALLER_MESSAGES: dict[type[ProviderError], str] = {
    BadRequest: "request rejected by upstream provider",
    AuthError: "authentication failed",
    ContentFiltered: "content filtered by upstream provider",
}


def caller_error_for(exc: ProviderError) -> tuple[int, ErrorBody]:
    """HTTP status + body for caller errors that should NOT be failed over.

    The message in the returned body is always a fixed canonical string; raw
    vendor SDK text from ``exc.vendor_detail`` is intentionally excluded.
    """
    if isinstance(exc, BadRequest):
        return 400, ErrorBody(
            type="invalid_request",
            message=_CALLER_MESSAGES[BadRequest],
            retryable=False,
        )
    if isinstance(exc, AuthError):
        return 401, ErrorBody(
            type="auth",
            message=_CALLER_MESSAGES[AuthError],
            retryable=False,
        )
    if isinstance(exc, ContentFiltered):
        return 400, ErrorBody(
            type="content_filtered",
            message=_CALLER_MESSAGES[ContentFiltered],
            retryable=False,
        )
    # Should not happen — the router filters retryables.
    return 500, ErrorBody(type="internal", message="upstream error", retryable=False)
