"""TDD: gofast52.manifest contracts (SPEC 4.9)."""
import json

import pytest

from gofast52.manifest import ManifestData, ManifestStore
from gofast52.scheduler import Segment


@pytest.fixture
def store(tmp_path, clock):
    return ManifestStore(state_dir=tmp_path, clock=clock, min_interval=2.0)


@pytest.fixture
def data(tmp_path):
    return ManifestData(
        total=1000, target=str(tmp_path / "file.bin"), workers=8,
        segment_mib=64, segments=[Segment(0, 0, 499, 500, "DONE", 0),
                                  Segment(1, 500, 999, 0, "WAIT", 0)],
    )


class TestRoundTrip:
    def test_save_then_load(self, store, data, clock):
        assert store.save(data, force=True) is True
        loaded = store.load()
        assert loaded is not None
        assert loaded.total == 1000
        assert loaded.version == 52
        assert len(loaded.segments) == 2
        assert loaded.segments[0].done == 500
        assert loaded.segments[1].status == "WAIT"

    def test_load_missing_returns_none(self, store):
        assert store.load() is None

    def test_load_corrupt_returns_none_and_keeps_file(self, store, data):
        store.save(data, force=True)
        store.path.write_text("{ not json !!!", encoding="utf-8")
        assert store.load() is None
        assert store.path.exists()  # untouched for recovery

    def test_no_tmp_left_after_save(self, store, data):
        store.save(data, force=True)
        leftovers = [p.name for p in store.state_dir.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []


class TestV51Compat:
    def test_loads_v51_manifest(self, tmp_path, clock):
        v51 = {
            "version": 5,
            "total": 300,
            "target": str(tmp_path / "old.bin"),
            "workers": 16,
            "segment_mib": 64,
            "segments": [
                {"index": 0, "start": 0, "end": 99, "done": 100, "status": "DONE", "retries": 0},
                {"index": 1, "start": 100, "end": 199, "done": 40, "status": "WAIT", "retries": 2},
                {"index": 2, "start": 200, "end": 299, "done": 0, "status": "WAIT", "retries": 0},
            ],
        }
        d = tmp_path / ".gofast_v5_old.bin"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(v51), encoding="utf-8")
        store = ManifestStore(state_dir=d, clock=clock)
        loaded = store.load()
        assert loaded is not None
        assert loaded.total == 300
        assert loaded.segments[1].done == 40
        assert loaded.segments[1].retries == 2

    def test_v52_manifest_readable_by_v51_semantics(self, store, data):
        store.save(data, force=True)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        # v5.1 reads: total/target/workers/segment_mib/segments (+Segment(**x))
        assert raw["total"] == 1000
        assert isinstance(raw["segments"][0], dict)
        seg = Segment(**raw["segments"][0])
        assert seg.total == 500


class TestThrottledSave:
    def test_second_save_within_interval_skipped(self, store, data, clock):
        assert store.save(data, force=True) is True
        data.segments[1].done = 100
        assert store.save(data) is False          # throttled
        assert store.load().segments[1].done == 0  # old value still on disk

    def test_save_after_interval_writes(self, store, data, clock):
        store.save(data, force=True)
        clock.sleep(2.5)
        data.segments[1].done = 100
        assert store.save(data) is True
        assert store.load().segments[1].done == 100

    def test_force_save_always_writes(self, store, data, clock):
        store.save(data, force=True)
        data.segments[1].done = 100
        assert store.save(data, force=True) is True
        assert store.load().segments[1].done == 100


class TestRejectsMismatch:
    def test_wrong_total_or_target_returns_none(self, tmp_path, clock):
        d = tmp_path / "state"
        d.mkdir()
        bad = {"version": 5, "total": "lots", "target": 123,
               "segments": [{"index": "a"}]}
        (d / "manifest.json").write_text(json.dumps(bad), encoding="utf-8")
        store = ManifestStore(state_dir=d, clock=clock)
        assert store.load() is None
