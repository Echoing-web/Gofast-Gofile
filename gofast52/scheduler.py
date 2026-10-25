"""Segment scheduler (SPEC 4.8): dynamic work claiming for worker threads."""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass
class Segment:
    index: int
    start: int
    end: int
    done: int = 0
    status: str = "WAIT"  # WAIT | DOWN | DONE
    retries: int = 0

    @property
    def total(self) -> int:
        return self.end - self.start + 1

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)


class SegmentScheduler:
    def __init__(self, segments: list[Segment], max_retries: int = 8):
        self._segments = sorted(segments, key=lambda s: s.start)
        self.max_retries = max_retries
        self._lock = threading.Lock()
        # normalize: fully-done segments are DONE; anything else (including
        # DOWN/RETRY/FAIL saved by a crashed run or by v5.1) is claimable WAIT
        # — a crash-saved manifest must always be resumable (review P0-1)
        for s in self._segments:
            if s.done >= s.total:
                s.done = s.total
                s.status = "DONE"
            elif s.status != "WAIT":
                s.status = "WAIT"

    # ------------------------------------------------------------- tail split
    def maybe_split(self, min_split_bytes: int, desired_parallelism: int) -> bool:
        """Split a large segment when workers would otherwise idle (v5.3 F1).

        - unclaimed (WAIT) segment: shrink it in place at the split point and
          append the right half as a new WAIT segment;
        - claimed (DOWN) segment: keep the owner's range untouched (its HTTP
          response covers the original range and must not be truncated) and
          append the un-downloaded tail as a new WAIT segment — the tail is
          fetched redundantly by another worker; overlapping writes carry
          identical bytes, so this is always data-safe.
        Split point respects the done prefix so no progress is lost.
        Returns True when a split happened.
        """
        with self._lock:
            active = sum(1 for s in self._segments
                         if s.status == "DOWN" and s.retries < self.max_retries)
            claimable = [s for s in self._segments
                         if s.status == "WAIT" and s.retries < self.max_retries]
            if active + len(claimable) >= desired_parallelism:
                return False
            candidates = [s for s in self._segments
                          if s.status != "DONE" and s.retries < self.max_retries
                          and (s.total - s.done) >= 2 * min_split_bytes]
            if not candidates:
                return False
            seg = max(candidates, key=lambda s: s.total - s.done)
            split_pos = seg.start + seg.done + (seg.total - seg.done) // 2
            if split_pos <= seg.start + seg.done or split_pos > seg.end:
                return False
            right = Segment(index=-1, start=split_pos, end=seg.end,
                            done=0, status="WAIT", retries=0)
            if seg.status == "WAIT":
                seg.end = split_pos - 1  # shrink the unclaimed left half
            # DOWN segments keep their original range (owner's response covers it)
            pos = self._segments.index(seg) + 1
            self._segments.insert(pos, right)
            self._segments.sort(key=lambda s: s.start)
            return True

    # ----------------------------------------------------------------- claiming
    def claim(self) -> Segment | None:
        with self._lock:
            for s in self._segments:
                if s.status == "WAIT" and s.retries < self.max_retries:
                    s.status = "DOWN"
                    return s
            return None

    def complete(self, seg: Segment) -> None:
        with self._lock:
            if seg.done != seg.total:
                raise ValueError(
                    f"segment {seg.index} incomplete: {seg.done}/{seg.total}")
            seg.status = "DONE"

    def requeue(self, seg: Segment, count_retry: bool = True) -> None:
        with self._lock:
            seg.status = "WAIT"
            if count_retry:
                seg.retries += 1

    def exhausted(self) -> bool:
        with self._lock:
            return not any(
                s.status != "DONE" and s.retries < self.max_retries
                for s in self._segments
            )

    # ------------------------------------------------------------------ queries
    def snapshot(self) -> list[Segment]:
        with self._lock:
            ordered = sorted(self._segments, key=lambda s: s.start)
            return [replace(s, index=i) for i, s in enumerate(ordered)]

    def bytes_done(self) -> int:
        with self._lock:
            return sum(s.done for s in self._segments)

    def bytes_total(self) -> int:
        with self._lock:
            return sum(s.total for s in self._segments)

    def status_counts(self) -> dict[str, int]:
        counts = {"WAIT": 0, "DOWN": 0, "DONE": 0, "FAIL": 0}
        with self._lock:
            for s in self._segments:
                counts[s.status] = counts.get(s.status, 0) + 1
        return counts
