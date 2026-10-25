"""Cross-validation tests: independent code paths must agree (v5.3 gate).

Each case compares the outputs of two (or three) independent implementations
against the same ground truth:
  X1  multi-segment engine  vs  single-connection engine  (same bytes)
  X2  recover scan of a fully-downloaded file vs engine's own bookkeeping
  X3  crash-simulate -> recover -> engine resume -> final vs original data
  X4  SegmentWriter vs write_at (positional write equivalence)
  X5  v5.1-style manifest round-trip vs v5.2 manifest (same semantic fields)
"""
import json

from gofast52 import atomicio
from gofast52.api import TokenCache
from gofast52.engine import DownloadEngine, EngineConfig
from gofast52.recover import recover_partial
from gofast52.timeutil import FakeClock
from tests.test_engine import SMALL, ScriptedServer, _ctx, allow_all_guard
from tests.test_transport import FakeResponse

MIB = 1024 * 1024


def build_engine(tmp_path, data, **cfg_over):
    clock = FakeClock(start=1_700_000_000.0)
    server = ScriptedServer(data)
    cfg = EngineConfig(url="https://gofile.io/d/abc123",
                       out=tmp_path / "downloads", workers=4,
                       segment_mib=1, req_rate=1000.0, **cfg_over)
    eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                         ui=False, guard=allow_all_guard(),
                         token_cache=TokenCache(tmp_path / "tok.json"))
    return eng, server, clock


def force_single_mode(server, data):
    orig = server.stream

    def stream_probe_200(url, headers=None, timeout=None, expect_range=True):
        rng = (headers or {}).get("Range")
        if rng == "bytes=0-0" and not server.override:
            return _ctx(FakeResponse(200, {"Content-Length": str(len(data))}, data))
        return orig(url, headers=headers, timeout=timeout, expect_range=expect_range)

    server.stream = stream_probe_200


class TestCrossValidation:
    def test_x1_multi_path_equals_single_path(self, tmp_path):
        # two completely different download paths, identical result
        eng_multi, _, _ = build_engine(tmp_path / "a", SMALL)
        target_multi = eng_multi.run()

        eng_single, server_single, _ = build_engine(tmp_path / "b", SMALL)
        force_single_mode(server_single, SMALL)
        target_single = eng_single.run()

        assert target_multi.read_bytes() == target_single.read_bytes() == SMALL

    def test_x2_recover_of_complete_file_matches_engine_truth(self, tmp_path):
        # a fully downloaded partial must scan as all-DONE — recover's
        # content-based verdict agreeing with the server's ground truth
        state = tmp_path / ".gofast_v5_file.bin"
        state.mkdir(parents=True)
        (state / "file.bin.partial").write_bytes(SMALL)
        out = recover_partial(state, segment_mib=1)
        m = json.loads(out.read_text(encoding="utf-8"))
        assert all(s["status"] == "DONE" for s in m["segments"])
        assert sum(s["done"] for s in m["segments"]) == len(SMALL)

    def test_x3_crash_recover_resume_closed_loop(self, tmp_path):
        # simulate a crash mid-download (holes), rebuild bookkeeping with
        # recover, finish with the engine, and require byte-exact equality
        # with the original data — three components cross-checking each other
        hole_start = MIB + 300 * 1024        # crash frontier in segment 1
        blob = (SMALL[:hole_start]
                + b"\x00" * (len(SMALL) - hole_start))
        state = tmp_path / "downloads" / ".gofast_v5_file.bin"
        state.mkdir(parents=True)
        (state / "file.bin.partial").write_bytes(blob)

        out = recover_partial(state, segment_mib=1)
        m = json.loads(out.read_text(encoding="utf-8"))
        # segment 0 fully present -> DONE; segment 1 partial -> WAIT with
        # a trusted prefix; segment 2 never started -> WAIT
        assert m["segments"][0]["status"] == "DONE"
        assert m["segments"][1]["status"] == "WAIT"
        assert 0 < m["segments"][1]["done"] < MIB
        assert m["segments"][2]["status"] == "WAIT"
        # point the manifest at the right target name for the engine
        m["target"] = str((tmp_path / "downloads" / "file.bin").resolve())
        (state / "manifest.json").write_text(json.dumps(m), encoding="utf-8")

        eng, server, _ = build_engine(tmp_path, SMALL)
        target = eng.run()
        assert target.read_bytes() == SMALL
        # the trusted prefix meant the engine did NOT re-download from zero
        starts = sorted(a for (_, a, _) in server.range_requests)
        assert min(starts) >= m["segments"][1]["done"] + MIB - 1

    def test_x4_segmentwriter_equals_write_at(self, tmp_path):
        payload = bytes(range(256)) * 4096  # 1 MiB, non-trivial pattern
        size = 3 * MIB + 11
        atomicio.ensure_sized_file(str(tmp_path), "a.bin", size)
        atomicio.ensure_sized_file(str(tmp_path), "b.bin", size)
        offsets = [0, 17, MIB, 2 * MIB + 5, size - 7]
        for off in offsets:
            atomicio.write_at(str(tmp_path), "a.bin", off, payload)
            with atomicio.SegmentWriter(str(tmp_path), "b.bin") as w:
                w.write(off, payload)
        assert (tmp_path / "a.bin").read_bytes() == (tmp_path / "b.bin").read_bytes()

    def test_x5_v51_and_v52_manifests_carry_same_semantics(self, tmp_path):
        # the same logical state expressed in v5.1 and v5.2 manifest formats
        # must load into identical scheduler behavior
        from gofast52.manifest import ManifestStore
        from gofast52.scheduler import SegmentScheduler
        from gofast52.timeutil import RealClock
        segs = [
            {"index": 0, "start": 0, "end": 99, "done": 100, "status": "DONE", "retries": 0},
            {"index": 1, "start": 100, "end": 199, "done": 40, "status": "WAIT", "retries": 1},
        ]
        for version in (5, 52):
            d = tmp_path / f"state{version}"
            d.mkdir()
            (d / "manifest.json").write_text(json.dumps({
                "version": version, "total": 200,
                "target": str(tmp_path / "f.bin"),
                "workers": 4, "segment_mib": 64, "segments": segs,
            }), encoding="utf-8")
            store = ManifestStore(d, RealClock())
            m = store.load()
            assert m is not None
            sched = SegmentScheduler(m.segments, max_retries=8)
            snap = sched.snapshot()
            assert snap[0].status == "DONE" and snap[0].done == 100
            assert snap[1].status == "WAIT" and snap[1].done == 40
            claimed = sched.claim()
            assert claimed.start == 100 and claimed.done == 40
