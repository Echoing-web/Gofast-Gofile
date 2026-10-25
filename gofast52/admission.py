"""Global admission controller (SPEC 4.7): every outbound request must pass here.

Combines, in order: circuit breaker -> token bucket -> dynamic concurrency
semaphore. Reports flow back from transport results and drive the AIMD
governor, whose live limit is pushed onto the semaphore.

Review round C hardening:
- half-open probe slots travel with the Permit: if a request ends without a
  congestion result (fatal/cancelled/unclassified), the slot is returned to
  the breaker instead of leaking (review P0-5);
- stale successes from requests that predate an OPEN are not counted as
  probe successes (review P2-13).
"""
from __future__ import annotations

import threading
from typing import Optional

from .breaker import CircuitBreaker
from .errors import LinkExpired, FatalError, OperationCancelled, Throttled, TransientError
from .governor import ConcurrencyGovernor
from .ratelimit import TokenBucket
from .timeutil import Clock, Sleeper

_WAIT_GRANULARITY = 0.25  # cancel-check granularity for semaphore waits


class DynamicBoundedSemaphore:
    """Bounded semaphore whose limit can be changed at runtime.

    Lowering the limit never revokes already-granted permits; new acquires
    simply wait until in-flight drops below the new limit.
    """

    def __init__(self, initial: int):
        if initial < 1:
            raise ValueError("initial limit must be >= 1")
        self._limit = initial
        self._in_flight = 0
        self._cond = threading.Condition()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    def set_limit(self, n: int) -> None:
        with self._cond:
            self._limit = max(1, n)
            self._cond.notify_all()

    def acquire(self, cancel: Optional[threading.Event] = None) -> None:
        with self._cond:
            while self._in_flight >= self._limit:
                if cancel is not None and cancel.is_set():
                    raise OperationCancelled("cancelled while waiting for a slot")
                self._cond.wait(_WAIT_GRANULARITY)
            self._in_flight += 1

    def release(self) -> None:
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cond.notify_all()


class Permit:
    """One admitted request. Reporting is once-only; release is idempotent.

    Releasing without reporting returns a consumed probe slot to the breaker.
    """

    def __init__(self, controller: "AdmissionController", is_probe: bool = False):
        self._controller = controller
        self._is_probe = is_probe
        self._reported = False
        self._released = False
        self._lock = threading.Lock()

    @property
    def is_probe(self) -> bool:
        return self._is_probe

    def report_success(self) -> None:
        self._report(lambda c: c.report_success(is_probe=self._is_probe))

    def report_throttled(self, retry_after: float | None = None) -> None:
        self._report(lambda c: c.report_throttled(retry_after))

    def report_transient(self) -> None:
        self._report(lambda c: c.report_transient())

    def _report(self, fn) -> None:
        with self._lock:
            if self._reported:
                return
            self._reported = True
        fn(self._controller)

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            if not self._reported and self._is_probe:
                self._controller.report_abandoned()
        self._controller.semaphore.release()


class AdmissionController:
    def __init__(
        self,
        bucket: TokenBucket,
        breaker: CircuitBreaker,
        governor: ConcurrencyGovernor,
        clock: Clock,
        sleeper: Sleeper,
        semaphore: DynamicBoundedSemaphore | None = None,
    ):
        self.bucket = bucket
        self.breaker = breaker
        self.governor = governor
        self.clock = clock
        self.sleeper = sleeper
        self.semaphore = semaphore or DynamicBoundedSemaphore(governor.limit)

    # --------------------------------------------------------------- admission
    def acquire(self, cancel: Optional[threading.Event] = None) -> Permit:
        is_probe = self.breaker.before_request(
            sleeper=self.sleeper, clock=self.clock, cancel=cancel)
        try:
            if cancel is not None and cancel.is_set():
                raise OperationCancelled("cancelled before token bucket")
            self.bucket.acquire(sleeper=self.sleeper, cancel=cancel)
            if cancel is not None and cancel.is_set():
                raise OperationCancelled("cancelled before concurrency slot")
            self.semaphore.acquire(cancel=cancel)
        except BaseException:
            if is_probe:
                self.breaker.on_abandoned()  # give the probe slot back
            raise
        return Permit(self, is_probe=is_probe)

    # ----------------------------------------------------------------- reports
    def report_throttled(self, retry_after: float | None = None) -> None:
        self.breaker.on_throttled(retry_after)
        self.governor.on_throttled()
        self.semaphore.set_limit(self.governor.limit)

    def report_transient(self) -> None:
        self.breaker.on_transient()
        self.governor.on_transient()
        self.semaphore.set_limit(self.governor.limit)

    def report_success(self, is_probe: bool = True) -> None:
        self.breaker.on_success(is_probe=is_probe)
        self.governor.on_success()
        self.semaphore.set_limit(self.governor.limit)

    def report_abandoned(self) -> None:
        self.breaker.on_abandoned()


def run_guarded(controller: "AdmissionController", fn, cancel=None):
    """Run one request through admission and classify its outcome.

    Success -> AIMD growth; Throttled/Transient -> breaker+governor penalty;
    Fatal/LinkExpired/Cancelled -> release only (not congestion signals).
    """
    permit = controller.acquire(cancel=cancel)
    try:
        result = fn()
    except Throttled as e:
        permit.report_throttled(e.retry_after)
        raise
    except TransientError:
        permit.report_transient()
        raise
    except (FatalError, LinkExpired, OperationCancelled):
        permit.release()
        raise
    else:
        permit.report_success()
        return result
    finally:
        permit.release()
