"""TDD: gofast52.admission contracts (SPEC 4.7)."""
import threading

import pytest

from gofast52.admission import AdmissionController, DynamicBoundedSemaphore
from gofast52.breaker import CircuitBreaker, CircuitState
from gofast52.errors import OperationCancelled
from gofast52.governor import ConcurrencyGovernor
from gofast52.ratelimit import TokenBucket


class TestDynamicBoundedSemaphore:
    def test_acquire_up_to_limit(self):
        s = DynamicBoundedSemaphore(3)
        for _ in range(3):
            s.acquire()
        assert s.in_flight == 3

    def test_extra_acquire_blocks_until_release(self):
        s = DynamicBoundedSemaphore(1)
        s.acquire()
        got = []

        def waiter():
            s.acquire()
            got.append(True)

        th = threading.Thread(target=waiter, daemon=True)
        th.start()
        th.join(0.3)
        assert th.is_alive()          # still blocked
        s.release()
        th.join(1.0)
        assert got == [True]

    def test_set_limit_lower_blocks_new_acquires(self):
        s = DynamicBoundedSemaphore(4)
        for _ in range(3):
            s.acquire()
        s.set_limit(2)                # in_flight 3 > new limit 2
        th = threading.Thread(target=lambda: s.acquire(), daemon=True)
        th.start()
        th.join(0.3)
        assert th.is_alive()          # still blocked: in_flight 3 >= 2
        s.release()                   # in_flight 2, still >= limit 2
        th.join(0.3)
        assert th.is_alive()
        s.release()                   # in_flight 1 < 2 -> allowed
        th.join(1.0)
        assert not th.is_alive()

    def test_set_limit_higher_unblocks(self):
        s = DynamicBoundedSemaphore(1)
        s.acquire()
        th = threading.Thread(target=lambda: s.acquire(), daemon=True)
        th.start()
        th.join(0.2)
        assert th.is_alive()
        s.set_limit(2)
        th.join(1.0)
        assert not th.is_alive()

    def test_cancel_unblocks_waiter(self):
        s = DynamicBoundedSemaphore(1)
        s.acquire()
        cancel = threading.Event()
        holder = {}

        def waiter():
            try:
                s.acquire(cancel=cancel)
                holder["done"] = True
            except OperationCancelled:
                holder["cancelled"] = True

        th = threading.Thread(target=waiter, daemon=True)
        th.start()
        th.join(0.2)
        cancel.set()
        th.join(1.0)
        assert holder.get("cancelled") is True

    def test_release_never_negative(self):
        s = DynamicBoundedSemaphore(2)
        s.release()
        s.release()
        assert s.in_flight == 0

    def test_concurrent_acquires_respect_limit(self):
        s = DynamicBoundedSemaphore(3)
        peak = 0
        lock = threading.Lock()

        def worker():
            nonlocal peak
            for _ in range(30):
                s.acquire()
                with lock:
                    peak = max(peak, s.in_flight)
                s.release()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak <= 3


@pytest.fixture
def controller(clock):
    bucket = TokenBucket(rate=100.0, burst=100, clock=clock)  # effectively loose
    breaker = CircuitBreaker(throttle_threshold=5, cooldown=60.0, probes=2, clock=clock)
    governor = ConcurrencyGovernor(initial=4, maximum=16, minimum=2, clock=clock)
    return AdmissionController(bucket=bucket, breaker=breaker, governor=governor,
                               clock=clock, sleeper=clock)


class TestAdmissionController:
    def test_acquire_healthy_returns_permit(self, controller):
        permit = controller.acquire()
        assert permit is not None
        assert controller.semaphore.in_flight == 1
        permit.release()
        assert controller.semaphore.in_flight == 0

    def test_throttle_reports_flow_to_all_components(self, controller):
        for _ in range(5):
            controller.report_throttled(retry_after=None)
        assert controller.breaker.state is CircuitState.OPEN
        assert controller.governor.limit == 2  # 4 -> 2 after enough throttles

    def test_open_breaker_blocks_acquire_until_cooldown(self, controller, clock):
        for _ in range(5):
            controller.report_throttled(retry_after=None)
        t0 = clock.now()
        permit = controller.acquire()   # sleeps virtually through cooldown
        assert clock.now() >= t0 + 60.0
        permit.release()

    def test_report_success_grows_limit_via_governor(self, controller, clock):
        clock.sleep(20.0)  # satisfy grow cooldown trivially
        for _ in range(10):
            controller.report_success()
        assert controller.governor.limit == 5
        assert controller.semaphore.limit == 5  # semaphore follows governor

    def test_permit_reports_once(self, controller):
        permit = controller.acquire()
        permit.report_success()
        permit.report_success()  # second call ignored
        permit.release()

    def test_cancel_during_breaker_open(self, controller, clock):
        from gofast52.errors import OperationCancelled
        for _ in range(5):
            controller.report_throttled(retry_after=None)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(OperationCancelled):
            controller.acquire(cancel=cancel)

    def test_rate_limit_applies(self, clock):
        bucket = TokenBucket(rate=4.0, burst=2, clock=clock)
        breaker = CircuitBreaker(clock=clock)
        governor = ConcurrencyGovernor(initial=8, maximum=8, minimum=1, clock=clock)
        c = AdmissionController(bucket=bucket, breaker=breaker, governor=governor,
                                clock=clock, sleeper=clock)
        t0 = clock.now()
        permits = [c.acquire() for _ in range(4)]
        assert clock.now() >= t0 + 0.5  # 2 free, then 2 x (1/4 s)
        for p in permits:
            p.release()
