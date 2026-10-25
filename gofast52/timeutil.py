"""Clock / Sleeper seams so all timing logic is deterministically testable (SPEC 4.1)."""
from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()


class RealSleeper:
    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Clock + Sleeper in one: sleep(t) advances virtual time instantly.

    Thread-safe: engine tests share one instance across real worker threads.
    """

    def __init__(self, start: float = 0.0):
        self._now = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"negative sleep: {seconds}")
        if seconds > 0:
            with self._lock:
                self._now += seconds
