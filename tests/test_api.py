"""TDD: gofast52.api contracts (SPEC 4.12)."""
import hashlib
import json

import pytest

from gofast52.admission import AdmissionController
from gofast52.api import GoFileApi, TokenCache, flatten, website_token
from gofast52.breaker import CircuitBreaker
from gofast52.errors import FatalError, Throttled
from gofast52.governor import ConcurrencyGovernor
from gofast52.ratelimit import TokenBucket
from gofast52.timeutil import FakeClock
from tests.test_transport import FakeResponse, FakeSession, AllowAllGuard


@pytest.fixture
def clock():
    return FakeClock(start=1_700_000_000.0)


@pytest.fixture
def admission(clock):
    return AdmissionController(
        bucket=TokenBucket(rate=1000.0, burst=1000, clock=clock),
        breaker=CircuitBreaker(clock=clock),
        governor=ConcurrencyGovernor(initial=8, maximum=8, minimum=1, clock=clock),
        clock=clock, sleeper=clock,
    )


def make_api(responses, clock, admission):
    session = FakeSession(responses)
    from gofast52.transport import RequestsTransport
    transport = RequestsTransport(session=session, guard=AllowAllGuard())
    return GoFileApi(transport=transport, admission=admission,
                     salt="testsalt", now_fn=lambda: clock.now())


class TestWebsiteToken:
    def test_matches_v51_formula(self, clock):
        ts = clock.now()
        slot = int(ts) // 14400
        raw = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "Chrome/140 Safari/537.36::en-US::tok::%d::testsalt" % slot)
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert website_token("tok", "testsalt", now_fn=lambda: ts) == expected


class TestCreateAccount:
    def test_ok(self, clock, admission):
        api = make_api([FakeResponse(200, json_data={"status": "ok", "data": {"token": "T1"}})],
                       clock, admission)
        assert api.create_account() == "T1"
        assert admission.semaphore.in_flight == 0  # permit released

    def test_rate_limited_json(self, clock, admission):
        api = make_api([FakeResponse(200, json_data={
            "status": "error", "data": {"error": "error-rateLimit"}}),
            FakeResponse(200, json_data={"status": "ok", "data": {"token": "T2"}})],
            clock, admission)
        with pytest.raises(Throttled):
            api.create_account()


class TestGetContent:
    def test_ok(self, clock, admission):
        api = make_api([FakeResponse(200, json_data={"status": "ok", "data": {"id": "X"}})],
                       clock, admission)
        assert api.get_content("X", token="tok")["id"] == "X"

    def test_error_unavailable_mentions_salt(self, clock, admission):
        api = make_api([FakeResponse(200, json_data={
            "status": "error", "data": {"error": "error-unavailable"}})],
            clock, admission)
        with pytest.raises(FatalError) as ei:
            api.get_content("X", token="tok")
        assert "GOFILE_WT_SALT" in str(ei.value)

    def test_error_rate_limit_throttled(self, clock, admission):
        api = make_api([FakeResponse(200, json_data={
            "status": "error", "data": {"error": "error-rateLimit"}})],
            clock, admission)
        with pytest.raises(Throttled):
            api.get_content("X", token="tok")

    def test_other_error_fatal(self, clock, admission):
        api = make_api([FakeResponse(200, json_data={
            "status": "error", "data": {"error": "error-notFound"}})],
            clock, admission)
        with pytest.raises(FatalError):
            api.get_content("X", token="tok")


class TestFlatten:
    def test_nested(self):
        tree = {"type": "folder", "children": {
            "a": {"type": "file", "name": "a.bin"},
            "b": {"type": "folder", "children": {
                "c": {"type": "file", "name": "c.bin"},
            }},
        }}
        files = flatten(tree)
        assert [f["name"] for f in files] == ["a.bin", "c.bin"]

    def test_single_file(self):
        assert flatten({"type": "file", "name": "x"}) == [{"type": "file", "name": "x"}]


class TestTokenCache:
    def test_missing_returns_none(self, tmp_path):
        assert TokenCache(tmp_path / "tok.json").load() is None

    def test_roundtrip(self, tmp_path):
        cache = TokenCache(tmp_path / "tok.json")
        cache.save("guest-token-1")
        assert TokenCache(tmp_path / "tok.json").load() == "guest-token-1"

    def test_malformed_returns_none(self, tmp_path):
        p = tmp_path / "tok.json"
        p.write_text("{broken", encoding="utf-8")
        assert TokenCache(p).load() is None

    def test_not_a_string_returns_none(self, tmp_path):
        p = tmp_path / "tok.json"
        p.write_text(json.dumps({"token": 12345}), encoding="utf-8")
        assert TokenCache(p).load() is None


class TestEnsureToken:
    def test_reuses_cached_token(self, clock, admission, tmp_path):
        cache = TokenCache(tmp_path / "tok.json")
        cache.save("cached-tok")
        api = make_api([FakeResponse(200, json_data={"status": "ok", "data": {"id": "X"}})],
                       clock, admission)
        api.token_cache = cache
        token = api.ensure_token()
        assert token == "cached-tok"
        api.get_content("X", token=token)  # one content call, still no /accounts call
        assert "/contents/X" in api.transport.session.calls[0]["url"]

    def test_creates_when_no_cache(self, clock, admission, tmp_path):
        api = make_api([FakeResponse(200, json_data={"status": "ok", "data": {"token": "NEW"}})],
                       clock, admission)
        api.token_cache = TokenCache(tmp_path / "tok.json")
        assert api.ensure_token() == "NEW"
        assert api.token_cache.load() == "NEW"

    def test_explicit_token_wins(self, clock, admission, tmp_path):
        api = make_api([], clock, admission)
        api.token_cache = TokenCache(tmp_path / "tok.json")
        assert api.ensure_token(explicit_token="premium-tok") == "premium-tok"
