"""HTTP transport port + requests adapter (SPEC 4.10).

Manual per-hop redirect following (each hop passes the URL guard), explicit
status classification into the SPEC 4.2 error taxonomy, urllib3 hidden
retries disabled (retry authority lives in the admission layer).
"""
from __future__ import annotations

import contextlib
import re
import time
from typing import Mapping, Optional
from urllib.parse import urljoin

import requests

from .consts import UA
from .errors import FatalError, LinkExpired, Throttled, TransientError
from .ssrf import URLGuard

CONNECT_TIMEOUT = 20.0
READ_TIMEOUT = 60.0
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d+)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


def parse_retry_after(value: Optional[str], now_epoch: Optional[float] = None) -> Optional[float]:
    """Parse Retry-After as integer seconds or HTTP-date; None if unusable."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    import email.utils  # lazy: only the HTTP-date path needs it (v5.3 slim)
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    now = now_epoch if now_epoch is not None else time.time()
    try:
        return max(0.0, dt.timestamp() - now)
    except (OverflowError, OSError, ValueError):
        return None


class RequestsTransport:
    def __init__(
        self,
        session: Optional[requests.Session] = None,
        pool_size: int = 16,
        guard: Optional[URLGuard] = None,
        max_redirects: int = 5,
        verify: bool = True,
    ):
        self.guard = guard or URLGuard()
        self.max_redirects = max_redirects
        self.verify = verify
        if session is None:
            session = requests.Session()
            session.verify = verify
            # v5.1 parity: the website-token hash binds the UA, so every
            # request (API included) must carry the same Chrome UA (review P1-7)
            session.headers.update({
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "Origin": "https://gofile.io",
                "Referer": "https://gofile.io/",
            })
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        self.session = session

    # ------------------------------------------------------------------ core
    def _send(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        json_body=None,
        stream: bool = False,
        timeout: tuple = (CONNECT_TIMEOUT, READ_TIMEOUT),
        expect_range: bool = True,
    ):
        current = url
        hops = 0
        while True:
            self.guard.validate(current)
            try:
                r = self.session.request(
                    method, current, headers=headers, json=json_body,
                    stream=stream, allow_redirects=False, timeout=timeout,
                )
            except requests.RequestException as e:
                raise TransientError(f"network error: {e}") from e
            if r.status_code in _REDIRECT_STATUSES:
                location = r.headers.get("Location")
                r.close()
                if not location:
                    raise TransientError(f"redirect ({r.status_code}) without Location")
                current = urljoin(current, location)
                hops += 1
                if hops > self.max_redirects:
                    raise TransientError("too many redirects")
                continue
            break
        try:
            self._classify(r, headers, expect_range=expect_range)
        except Exception:
            r.close()  # never leak an unread streamed response on the error path
            raise
        return r

    def _classify(self, r, request_headers: Optional[Mapping[str, str]],
                  expect_range: bool = True) -> None:
        status = r.status_code
        if status in (429, 503):
            raise Throttled(
                f"HTTP {status}",
                retry_after=parse_retry_after(r.headers.get("Retry-After")),
            )
        remaining = r.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.strip() == "0":
            raise Throttled("X-RateLimit-Remaining: 0", retry_after=None)
        if status in (403, 410):
            raise LinkExpired(f"HTTP {status} (direct link expired or forbidden)")
        if 400 <= status < 500:
            raise FatalError(f"HTTP {status}: {getattr(r, 'reason', None) or 'client error'}")
        if status >= 500:
            raise TransientError(f"HTTP {status}: {getattr(r, 'reason', None) or 'server error'}")
        rng = (request_headers or {}).get("Range")
        if rng and expect_range:
            if status != 206:
                raise TransientError(f"server ignored Range (HTTP {status})")
            m = _RANGE_RE.match(rng.strip())
            if m:
                want_start, want_end = int(m.group(1)), int(m.group(2))
                cr = r.headers.get("Content-Range", "")
                mm = _CONTENT_RANGE_RE.match(cr.strip())
                if not mm or int(mm.group(1)) != want_start or int(mm.group(2)) != want_end:
                    raise TransientError(
                        f"Content-Range mismatch: {cr!r} (wanted {rng})")

    # ----------------------------------------------------------------- ports
    @contextlib.contextmanager
    def stream(self, url, headers=None, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
               expect_range: bool = True):
        r = self._send("GET", url, headers=headers, stream=True, timeout=timeout,
                       expect_range=expect_range)
        try:
            yield r
        finally:
            r.close()

    def json_request(self, method, url, headers=None, json_body=None,
                     timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)):
        r = self._send(method, url, headers=headers, json_body=json_body,
                       stream=False, timeout=timeout)
        try:
            return r.json()
        except ValueError as e:
            raise TransientError(f"invalid JSON from {url}: {e}") from e
        finally:
            r.close()
