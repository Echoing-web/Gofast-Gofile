"""TDD: gofast52.recover contracts (SPEC 4.14; review round C prefix semantics).

Trust model: crashed segments have real-data prefixes and zero suffixes; the
scanner trusts only the provable prefix (leading non-zero sample windows plus
an end anchor). A segment is DONE only when every window — including the
end-anchored one — carries data.
"""
import json

import pytest

from gofast52.recover import recover_partial, scan_segment_prefix

MIB = 1024 * 1024
STRIDE = 256 * 1024
WINDOW = 16 * 1024


def make_partial(tmp_path, seg_bytes, seg_mib=1):
    state = tmp_path / ".gofast_v5_file.bin"
    state.mkdir(parents=True)
    partial = state / "file.bin.partial"
    partial.write_bytes(b"".join(seg_bytes))
    return state, partial


class TestScanPrefix:
    def test_full_data_segment_is_complete(self, tmp_path):
        f = tmp_path / "blob"
        f.write_bytes(b"\x01" * (2 * MIB))
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, 2 * MIB) == 2 * MIB

    def test_all_zero_segment_trusted_zero(self, tmp_path):
        f = tmp_path / "blob"
        f.write_bytes(b"\x00" * (2 * MIB))
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, 2 * MIB) == 0

    def test_crashed_half_segment_trusted_prefix(self, tmp_path):
        # 512 KiB real data then crash: trust ends inside the last verified
        # non-zero window ([256K, 256K+16K)) -> 272 KiB
        blob = b"\x01" * (512 * 1024) + b"\x00" * (512 * 1024)
        f = tmp_path / "blob"
        f.write_bytes(blob)
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, MIB) == STRIDE + WINDOW

    def test_zero_tail_beyond_window_not_trusted(self, tmp_path):
        # crash 32 KiB before the segment end: the end-anchored window is all
        # zero, so trust stops at the last stride window
        blob = b"\x01" * (MIB - 32 * 1024) + b"\x00" * (32 * 1024)
        f = tmp_path / "blob"
        f.write_bytes(blob)
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, MIB) < MIB

    def test_tiny_zero_tail_only_strict_catches(self, tmp_path):
        # crash 4 bytes before the end: inside the final sample window, the
        # sampled mode's documented residual; strict mode catches it exactly
        blob = b"\x01" * (MIB - 4) + b"\x00" * 4
        f = tmp_path / "blob"
        f.write_bytes(blob)
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, MIB) == MIB  # documented residual
            assert scan_segment_prefix(fp, 0, MIB, strict=True) == MIB - 4

    def test_island_between_windows_conservative(self, tmp_path):
        # data only between sample windows with leading zeros: not a prefix,
        # conservative verdict = trust nothing (redownload, never ship holes)
        blob = b"\x00" * (300 * 1024) + b"\x42" * 8 + b"\x00" * (2 * MIB)
        f = tmp_path / "blob"
        f.write_bytes(blob)
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, len(blob)) == 0

    def test_strict_full_scan_exact_last_nonzero(self, tmp_path):
        blob = b"\x01" * (512 * 1024) + b"\x00" * (512 * 1024)
        f = tmp_path / "blob"
        f.write_bytes(blob)
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, MIB, strict=True) == 512 * 1024

    def test_strict_full_data_complete(self, tmp_path):
        f = tmp_path / "blob"
        f.write_bytes(b"\x05" * MIB)
        with open(f, "rb") as fp:
            assert scan_segment_prefix(fp, 0, MIB, strict=True) == MIB


class TestRecoverPartial:
    def test_marks_zero_hole_segments_for_redownload(self, tmp_path):
        seg0 = bytes(range(256)) * (MIB // 256)   # full data
        seg1 = b"\x00" * MIB                       # never downloaded
        seg2 = b"\x22" * MIB                       # full data
        state, partial = make_partial(tmp_path, [seg0, seg1, seg2])

        out = recover_partial(state, segment_mib=1)
        m = json.loads(out.read_text(encoding="utf-8"))
        assert m["version"] == 52
        assert m["created_by"] == "gofast v5.2 recover"
        statuses = [s["status"] for s in m["segments"]]
        assert statuses == ["DONE", "WAIT", "DONE"]
        assert [s["done"] for s in m["segments"]] == [MIB, 0, MIB]
        assert m["total"] == 3 * MIB

    def test_crashed_prefix_segment_gets_partial_progress(self, tmp_path):
        # review P0-4 regression: half-written segment must NOT be DONE,
        # and its trusted prefix must be recorded for efficient resume
        seg0 = b"\x11" * MIB
        seg1 = b"\x22" * (512 * 1024) + b"\x00" * (512 * 1024)  # crashed mid-way
        state, partial = make_partial(tmp_path, [seg0, seg1])
        out = recover_partial(state, segment_mib=1)
        m = json.loads(out.read_text(encoding="utf-8"))
        assert m["segments"][0]["status"] == "DONE"
        assert m["segments"][1]["status"] == "WAIT"
        assert m["segments"][1]["done"] == STRIDE + WINDOW

    def test_partial_file_untouched(self, tmp_path):
        seg0 = b"\xaa" * MIB
        seg1 = b"\x00" * MIB
        state, partial = make_partial(tmp_path, [seg0, seg1])
        before = partial.read_bytes()
        recover_partial(state, segment_mib=1)
        assert partial.read_bytes() == before

    def test_uses_manifest_segment_size_and_warns_on_stale_total(self, tmp_path, capsys):
        # manifest total (2 MiB) is stale; the partial (3 MiB) is ground truth
        state, partial = make_partial(
            tmp_path, [b"\x11" * MIB, b"\x00" * MIB, b"\x00" * MIB])
        (state / "manifest.json").write_text(json.dumps({
            "version": 5, "total": 2 * MIB,
            "target": str(tmp_path / "file.bin"),
            "workers": 4, "segment_mib": 1,
            "segments": [
                {"index": 0, "start": 0, "end": MIB - 1, "done": 0, "status": "WAIT", "retries": 0},
                {"index": 1, "start": MIB, "end": 2 * MIB - 1, "done": 0, "status": "WAIT", "retries": 0},
            ],
        }), encoding="utf-8")
        out = recover_partial(state)
        m = json.loads(out.read_text(encoding="utf-8"))
        assert m["total"] == 3 * MIB            # partial size wins
        assert len(m["segments"]) == 3          # segment size from manifest
        assert m["segments"][0]["status"] == "DONE"
        assert m["segments"][2]["status"] == "WAIT"
        assert m["target"].endswith("file.bin")
        assert "不一致" in capsys.readouterr().out

    def test_no_partial_found_raises(self, tmp_path):
        state = tmp_path / ".gofast_v5_x"
        state.mkdir()
        from gofast52.errors import FatalError
        with pytest.raises(FatalError):
            recover_partial(state)

    def test_last_partial_segment_shorter(self, tmp_path):
        blob = b"\x01" * (MIB + 123)
        state, partial = make_partial(tmp_path, [blob])
        out = recover_partial(state, segment_mib=1)
        m = json.loads(out.read_text(encoding="utf-8"))
        assert len(m["segments"]) == 2
        assert m["segments"][1]["end"] - m["segments"][1]["start"] + 1 == 123
        assert m["segments"][1]["status"] == "DONE"
