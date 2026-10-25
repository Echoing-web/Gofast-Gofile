"""Crash-safe manifest persistence (SPEC 4.9), v5.1-compatible.

Reads never modify state; corrupt or foreign manifests yield None so the
recovery tooling can inspect them. Writes delegate entirely to
gofast52.atomicio (validated tmp+fsync+atomic replace inside the state dir),
with a configurable save throttle to avoid O(n^2) full-serialisation storms.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import atomicio
from .scheduler import Segment
from .timeutil import Clock

VERSION = 52
_COMPAT_VERSIONS = (5, VERSION)


@dataclass
class ManifestData:
    total: int
    target: str
    workers: int
    segment_mib: int
    segments: list[Segment]
    version: int = VERSION
    created_by: str = "gofast52"
    saved_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "total": self.total,
            "target": self.target,
            "workers": self.workers,
            "segment_mib": self.segment_mib,
            "created_by": self.created_by,
            "saved_at": self.saved_at,
            "segments": [
                {"index": s.index, "start": s.start, "end": s.end,
                 "done": s.done, "status": s.status, "retries": s.retries}
                for s in self.segments
            ],
        }

    @staticmethod
    def from_dict(d: object) -> "Optional[ManifestData]":
        if not isinstance(d, dict):
            return None
        if d.get("version") not in _COMPAT_VERSIONS:
            return None
        try:
            total = int(d["total"])
            target = str(d["target"])
            workers = int(d.get("workers", 8))
            segment_mib = int(d.get("segment_mib", 64))
            raw_segments = d["segments"]
            if total <= 0 or not isinstance(raw_segments, list):
                return None
            segments: list[Segment] = []
            for x in raw_segments:
                seg = Segment(
                    index=int(x["index"]), start=int(x["start"]), end=int(x["end"]),
                    done=max(0, int(x.get("done", 0))),
                    status=str(x.get("status", "WAIT")),
                    retries=max(0, int(x.get("retries", 0))),
                )
                if seg.end < seg.start:
                    return None
                segments.append(seg)
        except (KeyError, TypeError, ValueError):
            return None
        return ManifestData(
            total=total, target=target, workers=workers, segment_mib=segment_mib,
            segments=segments, version=int(d["version"]),
            created_by=str(d.get("created_by", "gofast52")),
            saved_at=d.get("saved_at"),
        )


class ManifestStore:
    def __init__(self, state_dir: Path, clock: Clock, min_interval: float = 2.0):
        raw = Path(state_dir)
        if os.path.pardir in raw.parts:
            raise ValueError(f"state_dir must stay inside its parent tree: {raw}")
        self.state_dir = raw.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.min_interval = min_interval
        self._last_save: Optional[float] = None
        self._io_lock = threading.Lock()  # serialize tmp+replace across threads

    @property
    def path(self) -> Path:
        return self.state_dir / "manifest.json"

    def load(self) -> Optional[ManifestData]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None  # corrupt: leave the file alone for recovery tooling
        return ManifestData.from_dict(data)

    def save(self, m: ManifestData, force: bool = False) -> bool:
        with self._io_lock:
            return self._save_locked(m, force)

    def _save_locked(self, m: ManifestData, force: bool) -> bool:
        now = self.clock.now()
        if (not force and self._last_save is not None
                and now - self._last_save < self.min_interval):
            return False
        m.saved_at = now
        atomicio.atomic_write_text(
            str(self.state_dir), "manifest.json", json.dumps(m.to_dict(), ensure_ascii=False)
        )
        self._last_save = now
        return True
