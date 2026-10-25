"""TDD: gofast52.scheduler contracts (SPEC 4.8)."""
import threading

import pytest

from gofast52.scheduler import Segment, SegmentScheduler


def make_segments(specs):
    """specs: list of (start, end, done)"""
    return [Segment(index=i, start=s, end=e, done=d)
            for i, (s, e, d) in enumerate(specs)]


@pytest.fixture
def sched():
    return SegmentScheduler(make_segments([(0, 99, 0), (100, 199, 0), (200, 299, 100)]),
                            max_retries=3)


class TestClaim:
    def test_claims_in_index_order(self, sched):
        s0 = sched.claim()
        s1 = sched.claim()
        assert (s0.index, s1.index) == (0, 1)

    def test_claim_skips_done_segments(self, sched):
        # segment 2 arrived fully done (done == total) -> never claimed
        claimed = [sched.claim() for _ in range(3)]
        assert [c.index for c in claimed[:2]] == [0, 1]
        assert claimed[2] is None

    def test_claim_returns_none_when_exhausted(self, sched):
        for _ in range(3):
            sched.claim()
        assert sched.claim() is None

    def test_concurrent_claims_no_duplicates(self):
        segs = make_segments([(i * 10, i * 10 + 9, 0) for i in range(50)])
        sched = SegmentScheduler(segs, max_retries=3)
        claimed = []
        lock = threading.Lock()

        def worker():
            while True:
                s = sched.claim()
                if s is None:
                    return
                with lock:
                    claimed.append(s.index)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(claimed) == list(range(50))  # exactly once each


class TestComplete:
    def test_complete_marks_done(self, sched):
        s = sched.claim()
        s.done = s.total
        sched.complete(s)
        assert s.status == "DONE"

    def test_complete_requires_full(self, sched):
        s = sched.claim()
        s.done = s.total - 1
        with pytest.raises(ValueError):
            sched.complete(s)

    def test_all_done_reports_exhausted(self):
        segs = make_segments([(0, 9, 10), (10, 19, 10)])
        sched = SegmentScheduler(segs, max_retries=3)
        assert sched.exhausted()
        assert sched.claim() is None


class TestRequeue:
    def test_requeue_preserves_done_and_increments_retries(self, sched):
        s = sched.claim()
        s.done = 50
        sched.requeue(s)
        assert s.status == "WAIT"
        assert s.done == 50
        assert s.retries == 1
        # index-order claiming: the requeued segment is first in line again
        again = sched.claim()
        assert again.index == 0
        assert again.done == 50       # partial progress preserved for resume

    def test_requeued_beyond_max_retries_not_reclaimed(self):
        segs = make_segments([(0, 99, 0)])
        sched = SegmentScheduler(segs, max_retries=2)
        s = sched.claim()
        sched.requeue(s)   # retries=1
        s2 = sched.claim()
        assert s2.index == 0
        sched.requeue(s2)  # retries=2 == max
        assert sched.claim() is None
        assert sched.exhausted()

    def test_dead_segment_does_not_block_others(self):
        segs = make_segments([(0, 99, 0), (100, 199, 50)])
        sched = SegmentScheduler(segs, max_retries=1)
        a = sched.claim()
        assert a.index == 0
        sched.requeue(a)               # retries=1 == max -> dead
        b = sched.claim()              # seg1 still claimable
        assert b.index == 1
        sched.requeue(b)               # dead too
        assert sched.claim() is None
        assert sched.exhausted()

    def test_exhausted_false_when_work_remains(self, sched):
        assert not sched.exhausted()


class TestTailSplit:
    """v5.3 F1: split big unclaimed segments when parallelism is starved."""

    MB = 1024 * 1024

    def test_split_when_starved(self):
        segs = [Segment(0, 0, 10 * self.MB - 1, done=10 * self.MB),
                Segment(1, 10 * self.MB, 20 * self.MB - 1)]
        sched = SegmentScheduler(segs, max_retries=3)
        assert sched.maybe_split(min_split_bytes=4 * self.MB,
                                 desired_parallelism=2) is True
        snap = sched.snapshot()
        assert len(snap) == 3
        assert snap[1].start == 10 * self.MB and snap[1].end == 15 * self.MB - 1
        assert snap[2].start == 15 * self.MB and snap[2].end == 20 * self.MB - 1
        assert snap[1].status == "WAIT" and snap[2].status == "WAIT"

    def test_no_split_when_parallelism_satisfied(self):
        segs = make_segments([(0, 10 * self.MB - 1, 0),
                              (10 * self.MB, 20 * self.MB - 1, 0)])
        sched = SegmentScheduler(segs, max_retries=3)
        assert sched.maybe_split(min_split_bytes=4 * self.MB,
                                 desired_parallelism=2) is False
        assert len(sched.snapshot()) == 2

    def test_no_split_when_remaining_too_small(self):
        segs = make_segments([(0, 7 * self.MB - 1, 0)])  # 7MB < 2*4MB
        sched = SegmentScheduler(segs, max_retries=3)
        assert sched.maybe_split(min_split_bytes=4 * self.MB,
                                 desired_parallelism=2) is False

    def test_split_point_respects_done_prefix(self):
        # 10MB segment with 6MB done: split lands at done + remaining/2 = 8MB,
        # so the left half keeps every done byte (no progress lost) and BOTH
        # halves carry new work
        segs = [Segment(0, 0, 10 * self.MB - 1, done=6 * self.MB)]
        sched = SegmentScheduler(segs, max_retries=3)
        assert sched.maybe_split(min_split_bytes=2 * self.MB,
                                 desired_parallelism=2) is True
        snap = sched.snapshot()
        assert snap[0].done == 6 * self.MB
        assert snap[0].end == 8 * self.MB - 1          # 6MB done + 2MB new
        assert snap[1].start == 8 * self.MB
        assert snap[1].end == 10 * self.MB - 1
        assert snap[1].done == 0

    def test_splits_claimed_down_segment_by_tail_steal(self):
        # owner keeps its full range (its in-flight response covers it); the
        # un-downloaded tail becomes a new WAIT segment for another worker
        segs = make_segments([(0, 10 * self.MB - 1, 0)])
        sched = SegmentScheduler(segs, max_retries=3)
        claimed = sched.claim()
        assert claimed.status == "DOWN"
        assert sched.maybe_split(min_split_bytes=2 * self.MB,
                                 desired_parallelism=2) is True
        snap = sched.snapshot()
        assert len(snap) == 2
        assert snap[0].start == 0 and snap[0].end == 10 * self.MB - 1  # owner intact
        assert snap[0].status == "DOWN"
        assert snap[1].start == 5 * self.MB and snap[1].end == 10 * self.MB - 1
        assert snap[1].status == "WAIT" and snap[1].done == 0

    def test_claim_order_and_reindex_after_split(self):
        segs = [Segment(0, 0, 8 * self.MB - 1)]
        sched = SegmentScheduler(segs, max_retries=3)
        sched.maybe_split(min_split_bytes=2 * self.MB, desired_parallelism=2)
        snap = sched.snapshot()
        # renumbered sequentially in start order
        assert [s.index for s in snap] == [0, 1]
        assert snap[0].end == 4 * self.MB - 1 and snap[1].start == 4 * self.MB
        a = sched.claim()
        b = sched.claim()
        assert a.start == 0 and b.start == 4 * self.MB

    def test_split_respects_retry_exhausted(self):
        segs = [Segment(0, 0, 10 * self.MB - 1, retries=3)]
        sched = SegmentScheduler(segs, max_retries=3)
        assert sched.maybe_split(min_split_bytes=2 * self.MB,
                                 desired_parallelism=2) is False


class TestSnapshot:
    def test_snapshot_is_deep_copy(self, sched):
        snap = sched.snapshot()
        snap[0].done = 999
        assert sched.snapshot()[0].done == 0

    def test_bytes_done_sums(self, sched):
        assert sched.bytes_done() == 100

    def test_bytes_total(self, sched):
        assert sched.bytes_total() == 300

    def test_counts_by_status(self, sched):
        sched.claim()
        counts = sched.status_counts()
        assert counts["WAIT"] == 1   # seg1
        assert counts["DOWN"] == 1   # seg0 claimed
        assert counts["DONE"] == 1   # seg2 arrived complete
