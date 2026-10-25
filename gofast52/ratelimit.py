"""Thread-safe token bucket (SPEC 4.4). Refills at `rate` tokens/s, capacity `burst`."""
from __future__ import annotations

import threading
from typing import Optional

from .errors import OperationCancelled
from .timeutil import Clock, Sleeper

_MIN_WAIT = 0.005  # avoid busy-loop at ~0 wait granularity
_CANCEL_GRANULARITY = 0.25  # cancel-check granularity while waiting


class TokenBucket:
    def __init__(self, rate: float, burst: int, clock: Clock):
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.rate = float(rate)
        self.burst = int(burst)
        self.clock = clock
        self._tokens = float(burst)
        self._last = clock.now()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = self.clock.now()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
        self._last = now

    def _take_wait_locked(self, tokens: int) -> float:
        """Consume up to `tokens` if available; return seconds to wait otherwise (consumes nothing)."""
        self._refill_locked()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        deficit = tokens - self._tokens
        return deficit / self.rate

    def try_acquire(self, tokens: int = 1) -> bool:
        with self._lock:
            return self._take_wait_locked(tokens) == 0.0

    def acquire(self, tokens: int = 1, *, sleeper: Sleeper,
                cancel: Optional[threading.Event] = None) -> None:
        while True:
            with self._lock:
                wait = self._take_wait_locked(tokens)
                if wait == 0.0:
                    return
            if cancel is not None and cancel.is_set():
                raise OperationCancelled("cancelled while waiting for tokens")
            sleeper.sleep(min(max(wait, _MIN_WAIT), _CANCEL_GRANULARITY))
