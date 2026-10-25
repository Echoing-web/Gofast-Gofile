"""Error taxonomy (SPEC 4.2). Transport translates facts into exactly these classes."""
from __future__ import annotations


class DownloadError(Exception):
    """Base class for all classified download errors."""

    kind = "error"


class Throttled(DownloadError):
    """HTTP 429 / 503 — the server is rate limiting us. retry_after in seconds, if provided."""

    kind = "throttled"

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(DownloadError):
    """Other 5xx, timeouts, connection errors, invalid 206 Content-Range. Retryable."""

    kind = "transient"


class LinkExpired(DownloadError):
    """CDN 403/410 — direct link expired; can be refreshed via the API."""

    kind = "link_expired"


class FatalError(DownloadError):
    """Unrecoverable: 400/401/403(API)/404, structural corruption, SSRF rejection."""

    kind = "fatal"


class OperationCancelled(DownloadError):
    """A blocking wait was cancelled (engine shutdown / Ctrl+C)."""

    kind = "cancelled"
