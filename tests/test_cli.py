"""TDD: gofast52.cli contracts (SPEC 4.16) + __main__ smoke."""
import json
import subprocess
import sys
from pathlib import Path


from gofast52 import cli


class TestParseDownload:
    def test_defaults(self):
        args = cli.parse(["download", "https://gofile.io/d/abc"])
        assert args.command == "download"
        assert args.workers == 16
        assert args.segment_mib == 64
        assert args.retries == 8
        assert args.req_rate == 4.0
        assert args.max_open_min == 15.0
        assert args.adaptive is True

    def test_v51_flags_still_work(self):
        args = cli.parse([
            "download", "https://gofile.io/d/abc",
            "--workers", "32", "--auto-workers", "--segment-mib", "128",
            "--retries", "4", "--password", "pw", "--out", "D:/x",
        ])
        assert args.workers == 32
        assert args.auto_workers is True
        assert args.segment_mib == 128
        assert args.password == "pw"

    def test_new_v52_flags(self):
        args = cli.parse([
            "download", "url", "--req-rate", "2", "--max-open-min", "30",
            "--no-adaptive", "--token", "prem",
        ])
        assert args.req_rate == 2.0
        assert args.max_open_min == 30.0
        assert args.adaptive is False
        assert args.token == "prem"

    def test_recover_subcommand(self):
        args = cli.parse(["recover", "--state-dir", "D:/st"])
        assert args.command == "recover"
        assert args.strict is False

    def test_repair_v5_subcommand(self):
        args = cli.parse(["repair-v5", "--v5-dir", "D:/st"])
        assert args.command == "repair-v5"

    def test_migrate_subcommand(self):
        args = cli.parse(["migrate-v2", "--v2-dir", "D:/v2"])
        assert args.command == "migrate-v2"


class TestMainDispatch:
    def test_download_invokes_engine(self, monkeypatch, tmp_path):
        recorded = {}

        class FakeEngine:
            def __init__(self, cfg):
                recorded["cfg"] = cfg

            def run(self):
                recorded["ran"] = True
                return Path("x")

        monkeypatch.setattr(cli, "ENGINE_CLS", FakeEngine)
        rc = cli.main(["download", "https://gofile.io/d/abc",
                       "--out", str(tmp_path)])
        assert rc == 0
        assert recorded["ran"] is True
        assert recorded["cfg"].url == "https://gofile.io/d/abc"
        # v5.2: adaptive AIMD must be on by default even without --auto-workers
        assert recorded["cfg"].adaptive is True
        assert recorded["cfg"].auto_workers is True
        # documented 8 MiB segment floor enforced at the CLI layer
        assert recorded["cfg"].segment_mib >= 8

    def test_download_fatal_error_rc1(self, monkeypatch, capsys):
        from gofast52.errors import FatalError

        class Boom:
            def __init__(self, cfg):
                pass

            def run(self):
                raise FatalError("nope")

        monkeypatch.setattr(cli, "ENGINE_CLS", Boom)
        rc = cli.main(["download", "https://gofile.io/d/abc"])
        assert rc == 1

    def test_repair_v5_extends_short_partial(self, tmp_path):
        state = tmp_path / ".gofast_v5_file.bin"
        state.mkdir()
        (state / "manifest.json").write_text(json.dumps({
            "version": 5, "total": 100, "target": str(tmp_path / "file.bin"),
            "workers": 4, "segment_mib": 64,
            "segments": [{"index": 0, "start": 0, "end": 99, "done": 0,
                          "status": "WAIT", "retries": 0}],
        }), encoding="utf-8")
        partial = state / "file.bin.partial"
        partial.write_bytes(b"x" * 40)
        rc = cli.main(["repair-v5", "--v5-dir", str(state)])
        assert rc == 0
        assert partial.stat().st_size == 100
        assert partial.read_bytes()[:40] == b"x" * 40  # existing bytes kept

    def test_recover_invoked(self, tmp_path):
        state = tmp_path / ".gofast_v5_file.bin"
        state.mkdir()
        (state / "file.bin.partial").write_bytes(b"\x00" * (1024 * 1024))
        rc = cli.main(["recover", "--state-dir", str(state), "--segment-mib", "1"])
        assert rc == 0
        assert (state / "manifest.json").exists()


class TestMainSmoke:
    def test_help_via_module(self):
        proc = subprocess.run(
            [sys.executable, "-m", "gofast52", "--help"],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0
        assert "download" in proc.stdout
        assert "recover" in proc.stdout

    def test_no_args_shows_help(self):
        rc = cli.main([])
        assert rc == 2
