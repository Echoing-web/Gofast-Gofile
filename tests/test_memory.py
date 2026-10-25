"""Memory / resource leak tests (v5.3 gate 4).

Deterministic, dependency-free leak detection:
- thread-count baseline stability across repeated engine runs (worker/watchdog/
  renderer threads must all be joined);
- tracemalloc heap growth bounded across repeated runs;
- no uncollectable cycles accumulated (gc.garbage stays empty);
- blocked admission waiters all exit after cancel (no stuck waiter leak).
"""
import gc
import threading
import tracemalloc

import pytest

from gofast52.admission import AdmissionController
from gofast52.breaker import CircuitBreaker
from gofast52.errors import OperationCancelled
from gofast52.governor import ConcurrencyGovernor
from gofast52.ratelimit import TokenBucket
from gofast52.timeutil import FakeClock
from tests.test_engine import SMALL, make_engine


def _run_once(tmp_path):
    eng, server, clock = make_engine(tmp_path, SMALL)
    eng.run()
    return eng


class TestThreadLeak:
    def test_thread_count_stable_across_runs(self, tmp_path):
        gc.collect()
        baseline = threading.active_count()
        for i in range(6):
            _run_once(tmp_path / f"run{i}")
            # give daemon threads a beat to fully exit
            for _ in range(20):
                if threading.active_count() <= baseline:
                    break
                import time as _t
                _t.sleep(0.02)
        assert threading.active_count() <= baseline

    def test_cancelled_waiters_do_not_leak(self):
        # block waiters on the concurrency semaphore (deterministic, no
        # half-open probe-slot races): one permit held, 8 waiters cancelled
        import time as _time
        controller = AdmissionController(
            bucket=TokenBucket(rate=10.0, burst=10, clock=FakeClock()),
            breaker=CircuitBreaker(clock=FakeClock()),
            governor=ConcurrencyGovernor(initial=1, maximum=1, minimum=1,
                                         clock=FakeClock()),
            clock=FakeClock(), sleeper=FakeClock())
        holder = controller.acquire()          # occupies the only slot
        cancel = threading.Event()
        survivors = []

        def waiter():
            try:
                controller.acquire(cancel=cancel)
            except OperationCancelled:
                survivors.append(threading.current_thread().ident)

        threads = [threading.Thread(target=waiter, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        _time.sleep(0.5)
        assert all(t.is_alive() for t in threads)  # all blocked on the slot
        cancel.set()
        for t in threads:
            t.join(3.0)
        assert not any(t.is_alive() for t in threads)
        assert len(survivors) == 8
        holder.release()


class TestMemoryLeak:
    def test_repeated_runs_bounded_heap_growth(self, tmp_path):
        # warmup (imports, first-run caches)
        for i in range(3):
            _run_once(tmp_path / f"warm{i}")
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.take_snapshot()
        for i in range(8):
            _run_once(tmp_path / f"run{i}")
            gc.collect()
        after = tracemalloc.take_snapshot()
        stats = after.compare_to(before, "filename")
        growth = sum(s.size_diff for s in stats if s.size_diff > 0)
        tracemalloc.stop()
        # generous bound: 8 full downloads must not grow the heap by >4 MiB
        assert growth < 4 * 1024 * 1024, (
            f"heap grew by {growth / 1024:.0f} KiB across 8 runs:\n"
            + "\n".join(f"{s.traceback.format()[-1]}: {s.size_diff/1024:.0f} KiB"
                        for s in stats if s.size_diff > 100 * 1024))

    def test_no_uncollectable_cycles_accumulate(self, tmp_path):
        gc.collect()
        before_garbage = len(gc.garbage)
        for i in range(5):
            _run_once(tmp_path / f"c{i}")
            gc.collect()
        assert len(gc.garbage) == before_garbage

    def test_scheduler_snapshots_do_not_accumulate(self):
        from gofast52.scheduler import Segment, SegmentScheduler
        segs = [Segment(i, i * 100, i * 100 + 99) for i in range(500)]
        sched = SegmentScheduler(segs, max_retries=3)
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(2000):
            sched.snapshot()
            sched.status_counts()
            sched.bytes_done()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert peak - before < 8 * 1024 * 1024  # no accumulation in the loop


class TestTransportResourceHygiene:
    def test_classified_error_responses_are_closed(self):
        # every error path through transport must close the response object
        from tests.test_transport import AllowAllGuard, FakeResponse, FakeSession
        from gofast52.transport import RequestsTransport
        responses = [FakeResponse(429, {"Retry-After": "1"}) for _ in range(50)]
        session = FakeSession(responses)
        t = RequestsTransport(session=session, guard=AllowAllGuard())
        from gofast52.errors import Throttled
        for _ in range(50):
            with pytest.raises(Throttled):
                with t.stream("https://cdn.example/f", headers={"Range": "bytes=0-9"}):
                    pass
        assert all(r.closed for r in responses)
