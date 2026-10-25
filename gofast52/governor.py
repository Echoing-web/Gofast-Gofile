"""AIMD concurrency governor (SPEC 4.6).

Additive increase (streak + cooldown gated), multiplicative decrease on
throttle, unit decrease on soft errors. Bounds clamped to [minimum, maximum].
"""
from __future__ import annotations

import math
import threading

from .timeutil import Clock


class ConcurrencyGovernor:
    def __init__(
        self,
        initial: int = 4,
        maximum: int = 16,
        minimum: int = 2,
        success_streak_to_grow: int = 10,
        grow_step: int = 1,
        decrease_factor: float = 0.5,
        grow_cooldown: float = 10.0,
        clock: Clock | None = None,
    ):
        if not (1 <= minimum <= initial <= maximum):
            raise ValueError("requires 1 <= minimum <= initial <= maximum")
        self.minimum = minimum
        self.maximum = maximum
        self.initial = initial
        self.success_streak_to_grow = success_streak_to_grow
        self.grow_step = grow_step
        self.decrease_factor = decrease_factor
        self.grow_cooldown = grow_cooldown
        self.clock = clock
        self._lock = threading.Lock()
        self._limit = initial
        self._streak = 0
        self._last_decrease: float | None = None

    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    @property
    def last_decrease(self) -> float | None:
        with self._lock:
            return self._last_decrease

    def on_success(self) -> int:
        with self._lock:
            self._streak += 1
            if self._streak < self.success_streak_to_grow:
                return self._limit
            if not self._growth_allowed_locked():
                return self._limit
            self._limit = min(self.maximum, self._limit + self.grow_step)
            self._streak = 0
            return self._limit

    def on_throttled(self) -> int:
        with self._lock:
            self._limit = max(self.minimum, math.ceil(self._limit * self.decrease_factor))
            self._streak = 0
            self._last_decrease = self._now()
            return self._limit

    def on_transient(self) -> int:
        with self._lock:
            self._limit = max(self.minimum, self._limit - 1)
            self._streak = 0
            self._last_decrease = self._now()
            return self._limit

    def _growth_allowed_locked(self) -> bool:
        if self._last_decrease is None:
            return True
        return (self._now() - self._last_decrease) >= self.grow_cooldown

    def _now(self) -> float:
        return self.clock.now() if self.clock is not None else 0.0
