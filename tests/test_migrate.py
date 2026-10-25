"""TDD: gofast52.migrate contracts (SPEC 4.15, v5.1-compatible v2 handoff)."""
import json

import pytest

from gofast52 import migrate
from gofast52.errors import FatalError

MIB = 1024 * 1024


def make_v2(tmp_path, parts, total=None):
    v2 = tmp_path / "v2state"
    v2.mkdir()
    segs = []
    for i, (start, end, blob) in enumerate(parts):
        segs.append({"index": i, "start": start, "end": end, "retry": 0})
        (v2 / f"{i:06d}.part").write_bytes(blob)
    target = tmp_path / "game.zip"
    if total is None:
        total = parts[-1][1] + 1  # manifest total = last segment end + 1 (v5.1 semantics)
    (v2 / "manifest.json").write_text(json.dumps({
        "total": total, "target": str(target),
        "segment_mib": 64, "segments": segs,
    }), encoding="utf-8")
    return v2, target


class TestMigrateV2:
    def test_copies_parts_at_offsets(self, tmp_path):
        part0 = b"\x01" * 100
        part1 = b"\x02" * 50
        v2, target = make_v2(tmp_path, [(0, 99, part0), (100, 149, part1)])
        migrate.migrate_v2(v2)
        state = target.parent / ".gofast_v5_game.zip"
        partial = state / "game.zip.partial"
        data = partial.read_bytes()
        assert len(data) == 150
        assert data[:100] == part0
        assert data[100:150] == part1
        m = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
        assert m["version"] == 52
        assert [s["status"] for s in m["segments"]] == ["DONE", "DONE"]

    def test_short_part_partial_progress(self, tmp_path):
        part0 = b"\x01" * 40          # only 40 of 100 expected
        v2, target = make_v2(tmp_path, [(0, 99, part0)])
        migrate.migrate_v2(v2)
        state = target.parent / ".gofast_v5_game.zip"
        m = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
        assert m["segments"][0]["done"] == 40
        assert m["segments"][0]["status"] == "WAIT"
        assert len((state / "game.zip.partial").read_bytes()) == 100

    def test_v2_directory_untouched(self, tmp_path):
        part0 = b"\x03" * 10
        v2, _ = make_v2(tmp_path, [(0, 9, part0)])
        before = {p.name: p.read_bytes() for p in v2.iterdir()}
        migrate.migrate_v2(v2)
        after = {p.name: p.read_bytes() for p in v2.iterdir()}
        assert before == after

    def test_missing_manifest_rejected(self, tmp_path):
        v2 = tmp_path / "empty"
        v2.mkdir()
        with pytest.raises(FatalError):
            migrate.migrate_v2(v2)

    def test_manifest_without_segments_rejected(self, tmp_path):
        v2 = tmp_path / "v2s"
        v2.mkdir()
        (v2 / "manifest.json").write_text(json.dumps(
            {"total": 10, "target": str(tmp_path / "x.bin")}), encoding="utf-8")
        with pytest.raises(FatalError):
            migrate.migrate_v2(v2)

    def test_existing_larger_partial_rejected(self, tmp_path):
        v2, target = make_v2(tmp_path, [(0, 9, b"\x01" * 10)])
        state = target.parent / ".gofast_v5_game.zip"
        state.mkdir(parents=True)
        (state / "game.zip.partial").write_bytes(b"\x00" * 20)  # larger than total
        with pytest.raises(FatalError):
            migrate.migrate_v2(v2)

    def test_running_processes_block_migration(self, tmp_path, monkeypatch):
        v2, _ = make_v2(tmp_path, [(0, 9, b"\x01" * 10)])
        monkeypatch.setattr(migrate, "windows_process_running", lambda pats: True)
        with pytest.raises(FatalError):
            migrate.migrate_v2(v2)

    def test_custom_v5_dir_and_name(self, tmp_path):
        v2, target = make_v2(tmp_path, [(0, 9, b"\x04" * 10)])
        custom = tmp_path / "custom_state"
        migrate.migrate_v2(v2, v5_dir=custom, force_name="renamed.bin")
        m = json.loads((custom / "manifest.json").read_text(encoding="utf-8"))
        assert m["target"].endswith("renamed.bin")
        assert (custom / "renamed.bin.partial").exists()
