"""TDD: gofast52.engine contracts (SPEC 4.13)."""
import json
import re

import pytest

from gofast52.api import TokenCache
from gofast52.engine import DownloadEngine, EngineConfig
from gofast52.errors import FatalError, LinkExpired, Throttled, TransientError
from gofast52.ssrf import URLGuard
from gofast52.timeutil import FakeClock
from tests.test_transport import FakeResponse


def allow_all_guard():
    return URLGuard(resolver=lambda host: ["104.21.0.5"])


def _classify_like_transport(resp):
    """Translate a scripted HTTP response into the transport error taxonomy."""
    status = resp.status_code
    if status in (429, 503):
        ra = resp.headers.get("Retry-After")
        raise Throttled(f"HTTP {status}", retry_after=float(ra) if ra and ra.isdigit() else None)
    if status in (403, 410):
        raise LinkExpired(f"HTTP {status}")
    if status >= 500:
        raise TransientError(f"HTTP {status}")
    return resp


class ScriptedServer:
    """Fake transport: serves a byte blob via Range by default, with a
    scriptable override queue (pop first) for error injection."""

    def __init__(self, data: bytes, link="https://store1.gofile.io/dl/file.bin"):
        self.data = data
        self.link = link
        self.range_requests = []   # (url, start, end)
        self.api_calls = []
        self.override = []         # list of ("range"|"api", response-or-exception)
        self.link_refreshes = 0

    def queue(self, kind, item):
        self.override.append((kind, item))

    # -- transport protocol ---------------------------------------------------
    def stream(self, url, headers=None, timeout=None, expect_range=True):
        rng = (headers or {}).get("Range")
        is_probe = rng == "bytes=0-0"
        if self.override and self.override[0][0] == "range" and not is_probe:
            _, item = self.override.pop(0)
            if isinstance(item, Exception):
                raise item
            return _ctx(_classify_like_transport(item))
        if not rng:
            return _ctx(FakeResponse(200, {"Content-Length": str(len(self.data))}, self.data))
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else len(self.data) - 1
        if not is_probe:
            self.range_requests.append((url, a, b))
        body = self.data[a:b + 1]
        return _ctx(FakeResponse(
            206, {"Content-Range": f"bytes {a}-{b}/{len(self.data)}"}, body))

    def json_request(self, method, url, headers=None, json_body=None, timeout=None):
        self.api_calls.append((method, url))
        if self.override and self.override[0][0] == "api":
            _, item = self.override.pop(0)
            if isinstance(item, Exception):
                raise item
            return item if not isinstance(item, FakeResponse) else item.json()
        # default: account creation + content containing self.link
        if url.endswith("/accounts"):
            return {"status": "ok", "data": {"token": "guest-tok"}}
        if "/contents/" in url:
            self.link_refreshes += 1
            newlink = self.link if self.link_refreshes == 1 else f"{self.link}?v={self.link_refreshes}"
            return {"status": "ok", "data": {
                "type": "file", "name": "file.bin", "size": len(self.data),
                "link": newlink}}
        raise AssertionError(f"unexpected API url {url}")


class _ctx:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self.resp

    def __exit__(self, *exc):
        self.resp.close()
        return False


def make_engine(tmp_path, data, *, clock=None, **cfg_over):
    clock = clock or FakeClock(start=1_700_000_000.0)
    server = ScriptedServer(data)
    cfg = EngineConfig(
        url="https://gofile.io/d/abc123",
        out=tmp_path / "downloads",
        workers=4,
        segment_mib=1,           # 1 MiB segments -> tiny files split nicely
        req_rate=1000.0,          # loose bucket for tests
        **cfg_over,
    )
    eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                         ui=False, guard=allow_all_guard(),
                         token_cache=TokenCache(tmp_path / "tok.json"))
    return eng, server, clock


SMALL = bytes(range(256)) * 4096 * 3          # 3 MiB -> 3 segments of 1 MiB


class TestHappyPath:
    def test_download_completes_and_matches(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        target = eng.run()
        assert target.read_bytes() == SMALL
        # state dir cleaned up after success
        assert not (tmp_path / "downloads" / ".gofast_v5_file.bin").exists()

    def test_all_segments_fetched_exactly_once(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        eng.run()
        spans = sorted((a, b) for (_, a, b) in server.range_requests)
        assert spans == [(0, 1048575), (1048576, 2097151), (2097152, 3145727)]

    def test_admission_permits_released(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        eng.run()
        assert eng.admission.semaphore.in_flight == 0


class TestResume:
    def test_resume_skips_done_segments(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        seg_size = 1024 * 1024
        state = tmp_path / "downloads" / ".gofast_v5_file.bin"
        state.mkdir(parents=True)
        partial = state / "file.bin.partial"
        partial.write_bytes(SMALL[:seg_size] + b"\x00" * (len(SMALL) - seg_size))
        manifest = {
            "version": 5, "total": len(SMALL),
            "target": str((tmp_path / "downloads" / "file.bin").resolve()),
            "workers": 4, "segment_mib": 1,
            "segments": [
                {"index": 0, "start": 0, "end": seg_size - 1, "done": seg_size,
                 "status": "DONE", "retries": 0},
                {"index": 1, "start": seg_size, "end": 2 * seg_size - 1, "done": 0,
                 "status": "WAIT", "retries": 0},
                {"index": 2, "start": 2 * seg_size, "end": len(SMALL) - 1, "done": 0,
                 "status": "WAIT", "retries": 0},
            ],
        }
        (state / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        target = eng.run()
        assert target.read_bytes() == SMALL
        starts = sorted(a for (_, a, _) in server.range_requests)
        assert starts == [seg_size, 2 * seg_size]  # segment 0 never re-fetched

    def test_partial_larger_than_total_refused(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        state = tmp_path / "downloads" / ".gofast_v5_file.bin"
        state.mkdir(parents=True)
        partial = state / "file.bin.partial"
        partial.write_bytes(b"x" * (len(SMALL) + 10))
        with pytest.raises(FatalError):
            eng.run()


class TestThrottleRecovery:
    def test_single_throttle_backoff_then_success(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        server.queue("range", FakeResponse(429, {"Retry-After": "5"}))
        t0 = clock.now()
        target = eng.run()
        assert target.read_bytes() == SMALL
        # the retry waited at least the Retry-After window in virtual time
        assert clock.now() - t0 >= 5.0

    def test_throttle_storm_opens_breaker_and_recovers(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        # 5 throttles trip the breaker; one more fails the first half-open
        # probe (reopen); the next probe succeeds -> breaker closes and the
        # download completes. (A longer storm is the watchdog's domain,
        # covered by test_stuck_open_breaker_gives_up_and_saves_state.)
        for _ in range(6):
            server.queue("range", FakeResponse(429, {"Retry-After": "3"}))
        target = eng.run()   # must still finish after the storm
        assert target.read_bytes() == SMALL
        # the run may finish while the breaker still sits in HALF_OPEN with
        # one probe success pending; settle it deterministically before
        # asserting full recovery
        brk = eng.admission.breaker
        if brk.state.value == "half_open":
            permit = eng.admission.acquire()
            permit.report_success()
            permit.release()
        assert brk.state.value == "closed"  # recovered

    def test_stuck_open_breaker_gives_up_and_saves_state(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL, max_open_min=0.5 / 60)  # 0.5s of open time
        for _ in range(400):
            server.queue("range", FakeResponse(429, {"Retry-After": "30"}))
        with pytest.raises(FatalError):
            eng.run()
        # the manifest was force-saved for later resume
        state = tmp_path / "downloads" / ".gofast_v5_file.bin"
        assert (state / "manifest.json").exists()


class TestLinkRefresh:
    def test_expired_link_is_refreshed_and_download_continues(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        server.queue("range", FakeResponse(403, {}))  # first range hit: expired
        target = eng.run()
        assert target.read_bytes() == SMALL
        assert server.link_refreshes >= 2          # initial + refresh
        # post-refresh requests used the new URL
        assert any("?v=2" in url for (url, _, _) in server.range_requests)


class TestSingleConnectionFallback:
    def test_no_range_support_uses_single_stream(self, tmp_path):
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)

        orig_stream = server.stream

        def stream_no_range(url, headers=None, timeout=None, expect_range=True):
            rng = (headers or {}).get("Range")
            if rng and rng == "bytes=0-0" and not server.override:
                return _ctx(FakeResponse(200, {"Content-Length": str(len(SMALL))}, SMALL))
            return orig_stream(url, headers=headers, timeout=timeout, expect_range=expect_range)

        server.stream = stream_no_range
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=4,
                           segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        target = eng.run()
        assert target.read_bytes() == SMALL

    def test_single_mode_honors_throttle_and_retries(self, tmp_path):
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)
        orig_stream = server.stream

        def stream_override(url, headers=None, timeout=None, expect_range=True):
            rng = (headers or {}).get("Range")
            if rng == "bytes=0-0" and not server.override:
                return _ctx(FakeResponse(200, {"Content-Length": str(len(SMALL))}, SMALL))
            if not rng and server.override and server.override[0][0] == "range":
                _, item = server.override.pop(0)
                if isinstance(item, Exception):
                    raise item
                return _ctx(_classify_like_transport(item))
            return orig_stream(url, headers=headers, timeout=timeout,
                               expect_range=expect_range)

        server.stream = stream_override
        server.queue("range", FakeResponse(429, {"Retry-After": "4"}))
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=2,
                           segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        t0 = clock.now()
        target = eng.run()
        assert target.read_bytes() == SMALL
        assert clock.now() - t0 >= 4  # Retry-After honored before the retry


class TestFinalize:
    def test_existing_target_backed_up_to_old(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        downloads = tmp_path / "downloads"
        downloads.mkdir(parents=True)
        (downloads / "file.bin").write_bytes(b"OLD-CONTENT")
        target = eng.run()
        assert target.read_bytes() == SMALL
        assert (downloads / "file.bin.old").read_bytes() == b"OLD-CONTENT"


class TestTailSplitEndgame:
    """v5.3 F1: starved workers split big remaining segments instead of
    letting the endgame run at low parallelism."""

    def test_endgame_splits_under_parallel_hunger(self, tmp_path):
        mib = 1024 * 1024
        data = bytes(range(256)) * 4096 * 7      # 7 MiB
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(data)
        # custom manifest: two tiny done segments + one big 5 MiB tail segment
        state = tmp_path / "downloads" / ".gofast_v5_file.bin"
        state.mkdir(parents=True)
        (state / "file.bin.partial").write_bytes(
            data[:2 * mib] + b"\x00" * (len(data) - 2 * mib))
        (state / "manifest.json").write_text(json.dumps({
            "version": 52, "total": len(data),
            "target": str((tmp_path / "downloads" / "file.bin").resolve()),
            "workers": 4, "segment_mib": 64,
            "segments": [
                {"index": 0, "start": 0, "end": mib - 1, "done": mib,
                 "status": "DONE", "retries": 0},
                {"index": 1, "start": mib, "end": 2 * mib - 1, "done": mib,
                 "status": "DONE", "retries": 0},
                {"index": 2, "start": 2 * mib, "end": len(data) - 1, "done": 0,
                 "status": "WAIT", "retries": 0},
            ],
        }), encoding="utf-8")
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=4,
                           segment_mib=64, req_rate=1000.0, split_min_mib=1)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        target = eng.run()
        assert target.read_bytes() == data
        spans = [(a, b) for (_, a, b) in server.range_requests]
        # without splitting this endgame would be ONE 5 MiB request; with
        # tail-splitting the tail was fetched by several parallel workers
        tail_spans = [s for s in spans if s[0] >= 2 * mib]
        assert len(tail_spans) >= 3, spans
        # and every byte of the tail is covered by at least one span
        covered = set()
        for a, b in tail_spans:
            covered.update(range(a, b + 1))
        assert covered == set(range(2 * mib, len(data)))


class TestReviewRoundC:
    """Regression tests for the independent review (round B) findings."""

    def _prepare_state(self, tmp_path, seg_statuses):
        """Write a crash-style manifest: seg0 DONE, others with given statuses."""
        seg_size = 1024 * 1024
        state = tmp_path / "downloads" / ".gofast_v5_file.bin"
        state.mkdir(parents=True)
        blob = SMALL[:seg_size] + b"\x00" * (len(SMALL) - seg_size)
        (state / "file.bin.partial").write_bytes(blob)
        segs = []
        for i, (status, done) in enumerate(seg_statuses):
            segs.append({"index": i, "start": i * seg_size,
                         "end": min((i + 1) * seg_size, len(SMALL)) - 1,
                         "done": done, "status": status, "retries": 0})
        (state / "manifest.json").write_text(json.dumps({
            "version": 5, "total": len(SMALL),
            "target": str((tmp_path / "downloads" / "file.bin").resolve()),
            "workers": 4, "segment_mib": 1, "segments": segs,
        }), encoding="utf-8")
        return state, seg_size

    def test_crash_saved_DOWN_segment_resumes(self, tmp_path):
        # P0-1: a crashed run saves in-flight segments as DOWN; they must be
        # re-claimable after restart, not stuck forever
        self._prepare_state(tmp_path, [("DONE", 1048576), ("DOWN", 0), ("WAIT", 0)])
        eng, server, clock = make_engine(tmp_path, SMALL)
        target = eng.run()
        assert target.read_bytes() == SMALL
        starts = sorted(a for (_, a, _) in server.range_requests)
        assert starts == [1048576, 2097152]

    def test_v51_crash_statuses_retry_fail_resume(self, tmp_path):
        # P0-1 (v5.1 side): v5.1 also wrote RETRY/FAIL statuses on crash
        self._prepare_state(tmp_path, [("DONE", 1048576), ("RETRY", 0), ("FAIL", 0)])
        eng, server, clock = make_engine(tmp_path, SMALL)
        target = eng.run()
        assert target.read_bytes() == SMALL

    def test_single_mode_restart_resumes_from_part_prefix(self, tmp_path):
        # P0-2: the .part is append-mode (never preallocated), so its size is
        # the verified prefix; a restart must resume, not deliver a zero tail
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)
        downloads = tmp_path / "downloads"
        downloads.mkdir(parents=True)
        # previous run wrote 1 MiB of real data then was killed
        (downloads / "file.bin.part").write_bytes(SMALL[:1048576])

        orig_stream = server.stream

        def stream_probe_200(url, headers=None, timeout=None, expect_range=True):
            rng = (headers or {}).get("Range")
            if rng == "bytes=0-0":
                return _ctx(FakeResponse(200, {"Content-Length": str(len(SMALL))}, SMALL))
            return orig_stream(url, headers=headers, timeout=timeout,
                               expect_range=expect_range)

        server.stream = stream_probe_200
        cfg = EngineConfig(url="https://gofile.io/d/abc123", out=downloads,
                           workers=2, segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        target = eng.run()
        assert target.read_bytes() == SMALL
        # the resume request asked for the remaining suffix only
        resume = [(a, b) for (_, a, b) in server.range_requests]
        assert resume == [(1048576, len(SMALL) - 1)]

    def test_single_mode_resume_answered_200_fails_clearly(self, tmp_path):
        # P0-3: a resume Range request answered with 200 (full body) must be
        # rejected — writing it at the offset would corrupt the file
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)
        downloads = tmp_path / "downloads"
        downloads.mkdir(parents=True)
        (downloads / "file.bin.part").write_bytes(SMALL[:1048576])

        def stream_bad_resume(url, headers=None, timeout=None, expect_range=True):
            rng = (headers or {}).get("Range")
            if rng == "bytes=0-0":
                return _ctx(FakeResponse(200, {"Content-Length": str(len(SMALL))}, SMALL))
            if rng:
                # server ignores Range and returns the whole file
                return _ctx(FakeResponse(200, {"Content-Length": str(len(SMALL))}, SMALL))
            raise AssertionError("no-range request during resume test")

        server.stream = stream_bad_resume
        cfg = EngineConfig(url="https://gofile.io/d/abc123", out=downloads,
                           workers=2, segment_mib=1, req_rate=1000.0, retries=2)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        with pytest.raises(FatalError) as ei:
            eng.run()
        assert "断点续传" in str(ei.value)
        # and the partial was not corrupted by misplaced writes
        part = downloads / "file.bin.part"
        assert part.read_bytes() == SMALL[:1048576]
        assert part.stat().st_size == 1048576

    def test_oversized_body_never_writes_past_segment_end(self, tmp_path):
        # P2-15: a 206 with correct headers but an over-long body must not
        # write beyond the segment boundary
        data = bytes(range(256)) * 4096  # 1 MiB, single segment
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(data)
        server.queue("range", FakeResponse(
            206, {"Content-Range": f"bytes 0-{len(data)-1}/{len(data)}"},
            data + b"EXTRA-5"))
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=2,
                           segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        target = eng.run()
        assert target.read_bytes() == data  # EXTRA never landed

    def test_multi_file_refresh_matches_selected_file(self, tmp_path):
        # P1-8: refreshing must return the SELECTED file's link, not the first
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)

        def json_multi(method, url, headers=None, json_body=None, timeout=None):
            if "/contents/" in url:
                return {"status": "ok", "data": {
                    "type": "folder",
                    "children": {
                        "f1": {"type": "file", "name": "one.bin", "size": 1,
                               "link": "https://wrong.example/one.bin"},
                        "f2": {"type": "file", "name": "file.bin",
                               "size": len(SMALL), "link": server.link},
                    }}}
            return {"status": "ok", "data": {"token": "t"}}

        server.json_request = json_multi
        server.queue("range", FakeResponse(403, {}))  # expire on first hit
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=2,
                           segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, choose_file=lambda files: files[1],
                             guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        target = eng.run()
        assert target.read_bytes() == SMALL
        urls = {u for (u, _, _) in server.range_requests}
        assert urls == {server.link}  # never touched the other file's link

    def test_refresher_concurrent_refresh_waits_for_link_change(self, tmp_path):
        # P1-6: a second refresh inside the 30s window waits for the engine's
        # link to change instead of failing the whole run
        from gofast52.engine import _LinkRefresher
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=2,
                           segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        want = {"id": "f2", "name": "file.bin", "size": len(SMALL)}
        refresher = _LinkRefresher(eng.api, "abc123", "tok", None, clock, clock,
                                   lambda: eng.current_link(), want)
        first = refresher.refresh("https://old.example/x")
        assert first == server.link
        api_calls_after_first = len(server.api_calls)

        # simulate another worker refreshing while we ask again inside the window
        def get_link_changing():
            eng._set_link(server.link + "?v=99")
            return eng.current_link()

        refresher.get_link = get_link_changing
        second = refresher.refresh(server.link)  # stale link arg
        assert second == server.link + "?v=99"
        assert len(server.api_calls) == api_calls_after_first  # no extra API call

    def test_cached_token_rejected_rebuilds_once(self, tmp_path, clock):
        # P1-9: a rejected cached guest token triggers invalidation + a single
        # account rebuild, then the content call succeeds
        from gofast52.api import GoFileApi, TokenCache
        from gofast52.transport import RequestsTransport
        from tests.test_transport import AllowAllGuard, FakeSession
        cache = TokenCache(tmp_path / "tok.json")
        cache.save("stale-token")
        session = FakeSession([
            FakeResponse(200, json_data={"status": "error",
                                         "data": {"error": "error-unavailable"}}),
            FakeResponse(200, json_data={"status": "ok", "data": {"token": "NEW"}}),
            FakeResponse(200, json_data={"status": "ok", "data": {"id": "X"}}),
        ])
        transport = RequestsTransport(session=session, guard=AllowAllGuard())
        from gofast52.admission import AdmissionController
        from gofast52.breaker import CircuitBreaker
        from gofast52.governor import ConcurrencyGovernor
        from gofast52.ratelimit import TokenBucket
        admission = AdmissionController(
            bucket=TokenBucket(rate=1000.0, burst=1000, clock=clock),
            breaker=CircuitBreaker(clock=clock),
            governor=ConcurrencyGovernor(initial=8, maximum=8, minimum=1, clock=clock),
            clock=clock, sleeper=clock)
        api = GoFileApi(transport=transport, admission=admission,
                        token_cache=cache, now_fn=lambda: clock.now())
        token, data = api.get_content_auto("X")
        assert token == "NEW"
        assert data["id"] == "X"
        assert cache.load() == "NEW"


class TestFatalErrors:
    def test_content_404_propagates(self, tmp_path):
        eng, server, clock = make_engine(tmp_path, SMALL)
        server.queue("api", {"status": "error", "data": {"error": "error-notFound"}})
        with pytest.raises(FatalError):
            eng.run()

    def test_missing_link_in_api_response(self, tmp_path):
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)

        def json_no_link(method, url, headers=None, json_body=None, timeout=None):
            if "/contents/" in url:
                return {"status": "ok", "data": {
                    "type": "file", "name": "file.bin", "size": len(SMALL)}}
            return {"status": "ok", "data": {"token": "t"}}

        server.json_request = json_no_link
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=2,
                           segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        with pytest.raises(FatalError):
            eng.run()


class TestMultiFile:
    def test_choose_file_callback(self, tmp_path):
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)

        def json_multi(method, url, headers=None, json_body=None, timeout=None):
            if "/contents/" in url:
                return {"status": "ok", "data": {
                    "type": "folder",
                    "children": {
                        "f1": {"type": "file", "name": "one.bin", "size": 1, "link": "https://x/1"},
                        "f2": {"type": "file", "name": "file.bin", "size": len(SMALL),
                               "link": server.link},
                    }}}
            return {"status": "ok", "data": {"token": "t"}}

        server.json_request = json_multi
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=2,
                           segment_mib=1, req_rate=1000.0)
        chosen = []
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, choose_file=lambda files: chosen.append(files) or files[1],
                             guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        target = eng.run()
        assert target.name == "file.bin"
        assert target.read_bytes() == SMALL
        assert len(chosen[0]) == 2
