"""Smoke tests (v5.3 gate 3): CLI subcommands end-to-end via subprocess.

No network: recover/repair-v5 operate on temp state dirs; --help covers the
remaining command surfaces; a full scripted download runs in-process through
the real engine wiring (fake transport) to prove the pieces assemble.
"""
import json
import subprocess
import sys
from pathlib import Path

MIB = 1024 * 1024


def run_cli(args, timeout=120):
    return subprocess.run(
        [sys.executable, "-m", "gofast52", *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(Path(__file__).resolve().parents[1]),
    )


class TestHelpSurfaces:
    def test_root_help(self):
        p = run_cli(["--help"])
        assert p.returncode == 0
        for cmd in ("download", "recover", "repair-v5", "migrate-v2"):
            assert cmd in p.stdout

    def test_download_help_mentions_new_flags(self):
        p = run_cli(["download", "--help"])
        assert p.returncode == 0
        for flag in ("--req-rate", "--max-open-min", "--no-adaptive", "--token", "--verbose"):
            assert flag in p.stdout


class TestRecoverSmoke:
    def test_recover_on_fresh_state(self, tmp_path):
        state = tmp_path / ".gofast_v5_file.bin"
        state.mkdir()
        (state / "file.bin.partial").write_bytes(
            b"\x11" * MIB + b"\x00" * MIB + b"\x22" * MIB)
        p = run_cli(["recover", "--state-dir", str(state), "--segment-mib", "1"])
        assert p.returncode == 0, p.stderr
        m = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
        assert [s["status"] for s in m["segments"]] == ["DONE", "WAIT", "DONE"]

    def test_recover_missing_dir_fails_cleanly(self, tmp_path):
        p = run_cli(["recover", "--state-dir", str(tmp_path / "nope")])
        assert p.returncode == 1
        assert "ERROR" in p.stderr


class TestRepairSmoke:
    def test_repair_extends_short_partial(self, tmp_path):
        state = tmp_path / ".gofast_v5_file.bin"
        state.mkdir()
        (state / "manifest.json").write_text(json.dumps({
            "version": 52, "total": 200, "target": str(tmp_path / "file.bin"),
            "workers": 4, "segment_mib": 64,
            "segments": [{"index": 0, "start": 0, "end": 199, "done": 0,
                          "status": "WAIT", "retries": 0}],
        }), encoding="utf-8")
        (state / "file.bin.partial").write_bytes(b"x" * 50)
        p = run_cli(["repair-v5", "--v5-dir", str(state)])
        assert p.returncode == 0, p.stderr
        assert (state / "file.bin.partial").stat().st_size == 200


class TestEndToEndDownload:
    def test_full_download_through_real_engine_wiring(self, tmp_path):
        """In-process smoke: real engine + admission stack, scripted server."""
        from tests.test_engine import SMALL, ScriptedServer, allow_all_guard
        from gofast52.api import TokenCache
        from gofast52.engine import DownloadEngine, EngineConfig
        from gofast52.timeutil import FakeClock
        clock = FakeClock(start=1_700_000_000.0)
        server = ScriptedServer(SMALL)
        cfg = EngineConfig(url="https://gofile.io/d/abc123",
                           out=tmp_path / "downloads", workers=4,
                           segment_mib=1, req_rate=1000.0)
        eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                             ui=False, guard=allow_all_guard(),
                             token_cache=TokenCache(tmp_path / "tok.json"))
        target = eng.run()
        assert target.read_bytes() == SMALL
        assert eng.admission.semaphore.in_flight == 0
