"""TDD: gofast52.atomicio SegmentWriter (v5.3 F2: one open per segment)."""
import pytest

from gofast52 import atomicio


class TestSegmentWriter:
    def test_writes_like_write_at(self, tmp_path):
        blob_a = b"A" * 1000
        blob_b = b"B" * 500
        atomicio.ensure_sized_file(str(tmp_path), "one.bin", 4096)
        with atomicio.SegmentWriter(str(tmp_path), "one.bin") as w:
            w.write(100, blob_a)
            w.write(3000, blob_b)
        data = (tmp_path / "one.bin").read_bytes()
        assert data[100:1100] == blob_a
        assert data[3000:3500] == blob_b
        assert data[0:100] == b"\x00" * 100

    def test_matches_write_at_exactly(self, tmp_path):
        payload = bytes(range(256)) * 64
        atomicio.ensure_sized_file(str(tmp_path), "x.bin", 16 * 1024 + 7)
        with atomicio.SegmentWriter(str(tmp_path), "x.bin") as w:
            w.write(7, payload)
        ref = tmp_path / "ref.bin"
        ref.write_bytes(b"\x00" * (16 * 1024 + 7))
        atomicio.write_at(str(tmp_path), "ref.bin", 7, payload)
        assert (tmp_path / "x.bin").read_bytes() == ref.read_bytes()

    def test_rejects_bad_filename(self, tmp_path):
        with pytest.raises(ValueError):
            atomicio.SegmentWriter(str(tmp_path), "../escape.bin")

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(OSError):
            atomicio.SegmentWriter(str(tmp_path), "missing.bin").__enter__()

    def test_closed_after_context(self, tmp_path):
        atomicio.ensure_sized_file(str(tmp_path), "c.bin", 16)
        w = atomicio.SegmentWriter(str(tmp_path), "c.bin")
        with w:
            pass
        assert w._fd is None
        with pytest.raises(ValueError):
            w.write(0, b"x")  # writing after close is a programming error
