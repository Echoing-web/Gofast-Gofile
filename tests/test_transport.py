"""TDD: gofast52.transport contracts (SPEC 4.10)."""
import pytest
import requests as requests_mod

from gofast52.errors import FatalError, LinkExpired, Throttled, TransientError
from gofast52.transport import RequestsTransport, parse_retry_after


class TestParseRetryAfter:
    def test_integer_seconds(self):
        assert parse_retry_after("120") == 120.0

    def test_zero(self):
        assert parse_retry_after("0") == 0.0

    def test_http_date_future(self):
        # fixed 'now': 2015-10-21 07:00:00 UTC
        now = 1445410800.0
        # Retry-After: 07:28:00 = +28min = 1680s
        assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now_epoch=now) == 1680.0

    def test_http_date_past_clamps_zero(self):
        now = 1445410800.0
        assert parse_retry_after("Wed, 21 Oct 2015 06:00:00 GMT", now_epoch=now) == 0.0

    def test_garbage_returns_none(self):
        assert parse_retry_after("soon-ish") is None
        assert parse_retry_after("") is None
        assert parse_retry_after(None) is None


class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b"", json_data=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._json = json_data
        self.closed = False

    def iter_content(self, chunk_size=1):
        view = memoryview(self._body)
        for i in range(0, len(view), chunk_size):
            yield bytes(view[i:i + chunk_size])

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeSession:
    """Scripted session.request(); records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, stream=False,
                allow_redirects=True, timeout=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "stream": stream, "json": json})
        if not self.responses:
            raise AssertionError("script exhausted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class AllowAllGuard:
    def validate(self, url):
        return None


def make_transport(responses, **kw):
    session = FakeSession(responses)
    t = RequestsTransport(session=session, guard=AllowAllGuard(), **kw)
    return t, session


RANGE = {"Range": "bytes=100-199"}


class TestStreamHappy:
    def test_206_with_matching_content_range(self):
        t, s = make_transport([FakeResponse(206, {"Content-Range": "bytes 100-199/1000"}, b"x" * 100)])
        with t.stream("https://cdn.example/f", headers=RANGE) as r:
            assert r.status_code == 206
            data = b"".join(r.iter_content(10))
        assert data == b"x" * 100
        assert s.calls[0]["stream"] is True

    def test_no_range_header_accepts_200(self):
        t, s = make_transport([FakeResponse(200, {}, b"hello")])
        with t.stream("https://cdn.example/f") as r:
            assert r.status_code == 200


class TestStatusClassification:
    def test_429_with_retry_after(self):
        t, _ = make_transport([FakeResponse(429, {"Retry-After": "30"})])
        with pytest.raises(Throttled) as ei:
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass
        assert ei.value.retry_after == 30.0

    def test_503_throttled(self):
        t, _ = make_transport([FakeResponse(503, {})])
        with pytest.raises(Throttled):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass

    def test_403_link_expired(self):
        t, _ = make_transport([FakeResponse(403, {})])
        with pytest.raises(LinkExpired):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass

    def test_410_link_expired(self):
        t, _ = make_transport([FakeResponse(410, {})])
        with pytest.raises(LinkExpired):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass

    def test_404_fatal(self):
        t, _ = make_transport([FakeResponse(404, {})])
        with pytest.raises(FatalError):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass

    def test_500_transient(self):
        t, _ = make_transport([FakeResponse(500, {})])
        with pytest.raises(TransientError):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass

    def test_range_request_answered_200_transient(self):
        t, _ = make_transport([FakeResponse(200, {}, b"full-file")])
        with pytest.raises(TransientError):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass

    def test_wrong_content_range_start_transient(self):
        t, _ = make_transport([FakeResponse(206, {"Content-Range": "bytes 0-199/1000"}, b"x" * 200)])
        with pytest.raises(TransientError):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass


class TestNetworkErrors:
    def test_connection_error_transient(self):
        t, _ = make_transport([requests_mod.ConnectionError("boom")])
        with pytest.raises(TransientError):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass

    def test_timeout_transient(self):
        t, _ = make_transport([requests_mod.Timeout("slow")])
        with pytest.raises(TransientError):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass


class TestRedirects:
    def test_follows_redirect_preserving_headers(self):
        t, s = make_transport([
            FakeResponse(302, {"Location": "https://cdn2.example/f"}),
            FakeResponse(206, {"Content-Range": "bytes 100-199/1000"}, b"y" * 100),
        ])
        with t.stream("https://cdn.example/f", headers=RANGE) as r:
            assert r.status_code == 206
        assert s.calls[0]["url"] == "https://cdn.example/f"
        assert s.calls[1]["url"] == "https://cdn2.example/f"
        assert s.calls[1]["headers"].get("Range") == "bytes=100-199"

    def test_redirect_to_private_blocked(self):
        class GuardRejectingPrivate:
            def validate(self, url):
                if "10.0.0" in url:
                    raise FatalError("private")
        session = FakeSession([
            FakeResponse(302, {"Location": "http://10.0.0.5/steal"}),
        ])
        t = RequestsTransport(session=session, guard=GuardRejectingPrivate())
        with pytest.raises(FatalError):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass
        assert len(session.calls) == 1  # never touched the private target

    def test_redirect_loop_transient(self):
        resp = FakeResponse(302, {"Location": "https://cdn.example/loop"})
        t, _ = make_transport([resp] * 8)
        with pytest.raises(TransientError):
            with t.stream("https://cdn.example/loop", headers=RANGE):
                pass


class TestJsonRequest:
    def test_ok_json(self):
        t, _ = make_transport([FakeResponse(200, json_data={"status": "ok", "data": {}})])
        out = t.json_request("GET", "https://api.example/x")
        assert out["status"] == "ok"

    def test_429_json_throttled(self):
        t, _ = make_transport([FakeResponse(429, {"Retry-After": "17"}, json_data={})])
        with pytest.raises(Throttled) as ei:
            t.json_request("GET", "https://api.example/x")
        assert ei.value.retry_after == 17.0


class TestRealSessionConstruction:
    def test_default_session_pool_sized(self):
        t = RequestsTransport(pool_size=23, guard=AllowAllGuard())
        adapter = t.session.get_adapter("https://cdn.example/x")
        assert adapter.poolmanager.connection_pool_kw["maxsize"] == 23
        # urllib3 hidden retries disabled: retry lives in admission
        assert adapter.max_retries.total == 0

    def test_default_session_carries_v51_headers(self):
        # review P1-7: the website-token hash binds the UA, so the session
        # must default to the same Chrome UA as v5.1 on every request
        t = RequestsTransport(pool_size=8, guard=AllowAllGuard())
        assert "Chrome" in t.session.headers["User-Agent"]
        assert t.session.headers["Origin"] == "https://gofile.io"
        assert t.session.headers["Referer"] == "https://gofile.io/"

    def test_x_ratelimit_remaining_zero_throttles(self):
        t, _ = make_transport([FakeResponse(200, {"X-RateLimit-Remaining": "0"}, b"")])
        with pytest.raises(Throttled):
            t.json_request("GET", "https://api.example/x")

    def test_response_closed_when_classification_raises(self):
        resp = FakeResponse(429, {"Retry-After": "9"})
        t, _ = make_transport([resp])
        with pytest.raises(Throttled):
            with t.stream("https://cdn.example/f", headers=RANGE):
                pass
        assert resp.closed is True  # no leaked streamed response
