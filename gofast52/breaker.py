"""Circuit breaker with CLOSED/OPEN/HALF_OPEN states (SPEC 4.5).

Defaults researched from resilience4j (60s open wait) and pybreaker
(5 consecutive failures), tuned for a segmented downloader: 2 half-open
probes, reopen cooldown x1.5 capped at 10 minutes.
"""
from __future__ import annotations

import threading
from enum import Enum

from .timeutil import Clock, Sleeper

_HALF_OPEN_POLL = 0.25  # wait granularity while other probes are in flight
_CANCEL_GRANULARITY = 0.25  # cancel-check granularity for cooldown waits
_MIN_STATED_COOLDOWN = 2.0  # floor when honoring a small stated Retry-After


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        throttle_threshold: int = 5,
        transient_threshold: int = 8,
        cooldown: float = 60.0,
        probes: int = 2,
        cooldown_backoff: float = 1.5,
        max_cooldown: float = 600.0,
        clock: Clock | None = None,
    ):
        if throttle_threshold < 1 or transient_threshold < 1 or probes < 1:
            raise ValueError("thresholds and probes must be >= 1")
        self.throttle_threshold = throttle_threshold
        self.transient_threshold = transient_threshold
        self.cooldown = cooldown
        self.probes = probes
        self.cooldown_backoff = cooldown_backoff
        self.max_cooldown = max_cooldown
        self.clock = clock  # may be None; only used by helpers needing time
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_throttle = 0
        self._consecutive_transient = 0
        self._open_until: float | None = None
        self._cooldown_current = cooldown
        self._probe_slots = 0
        self._probe_successes = 0
        self.total_stuck_cooldown = 0.0  # sum of cooldowns since last CLOSED
        self._last_retry_after: float | None = None  # server's latest signal

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def open_until(self) -> float | None:
        with self._lock:
            return self._open_until if self._state is CircuitState.OPEN else None

    # --------------------------------------------------------------- admission
    def before_request(self, sleeper: Sleeper, clock: Clock | None = None,
                       cancel: "threading.Event | None" = None) -> bool:
        """Block until this caller may issue a request. Virtual-time friendly.

        Returns True when this admission consumed a half-open probe slot.
        Raises OperationCancelled if `cancel` is set while waiting.
        """
        clk = clock if clock is not None else self.clock
        if clk is None:
            raise ValueError("before_request needs a clock (argument or constructor)")
        while True:
            with self._lock:
                if self._state is CircuitState.CLOSED:
                    return False
                if self._state is CircuitState.OPEN:
                    now = clk.now()
                    remaining = (self._open_until or now) - now
                    if remaining > 0:
                        wait = min(remaining, _CANCEL_GRANULARITY)
                    else:
                        self._enter_half_open_locked()
                        continue
                else:  # HALF_OPEN
                    if self._probe_slots > 0:
                        self._probe_slots -= 1
                        return True
                    wait = _HALF_OPEN_POLL
            if cancel is not None and cancel.is_set():
                from .errors import OperationCancelled
                raise OperationCancelled("cancelled while waiting for circuit")
            sleeper.sleep(max(wait, 0.0))

    def _enter_half_open_locked(self) -> None:
        self._state = CircuitState.HALF_OPEN
        self._probe_slots = self.probes
        self._probe_successes = 0

    # ----------------------------------------------------------------- reports
    def on_throttled(self, retry_after: float | None = None) -> None:
        with self._lock:
            self._last_retry_after = retry_after
            if self._state is CircuitState.OPEN:
                return  # late report from an in-flight request
            if self._state is CircuitState.HALF_OPEN:
                self._reopen_locked()
                return
            self._consecutive_throttle += 1
            self._consecutive_transient = 0
            if self._consecutive_throttle >= self.throttle_threshold:
                self._open_locked()

    def on_transient(self) -> None:
        with self._lock:
            if self._state is CircuitState.OPEN:
                return
            if self._state is CircuitState.HALF_OPEN:
                self._reopen_locked()
                return
            self._consecutive_transient += 1
            self._consecutive_throttle = 0
            if self._consecutive_transient >= self.transient_threshold:
                self._last_retry_after = None  # no server instruction here
                self._open_locked()

    def on_success(self, is_probe: bool = True) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                if not is_probe:
                    return  # stale in-flight success predating the open; not a probe
                self._probe_successes += 1
                if self._probe_successes >= self.probes:
                    self._close_locked()
                return
            if self._state is CircuitState.CLOSED:
                self._consecutive_throttle = 0
                self._consecutive_transient = 0

    def on_abandoned(self) -> None:
        """A request that consumed a probe slot ended without a congestion
        result (fatal/cancelled/unclassified). Return the slot so another
        waiter can probe instead of deadlocking in HALF_OPEN (review P0-5)."""
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._probe_slots = min(self.probes, self._probe_slots + 1)

    # ------------------------------------------------------------- transitions
    def _open_locked(self) -> None:
        # a stated Retry-After is the server's own instruction and wins;
        # otherwise use the base cooldown
        if self._last_retry_after is not None:
            self._cooldown_current = self._clamp_cooldown(self._last_retry_after)
        else:
            self._cooldown_current = self.cooldown
        self._state = CircuitState.OPEN
        self._open_until = self._now() + self._cooldown_current
        self.total_stuck_cooldown += self._cooldown_current

    def _reopen_locked(self) -> None:
        if self._last_retry_after is not None:
            # honor the server's fresh signal instead of escalating blindly
            # (a "wait 2s" answer must not turn into minutes of cooldown)
            self._cooldown_current = self._clamp_cooldown(self._last_retry_after)
        else:
            self._cooldown_current = min(
                self._cooldown_current * self.cooldown_backoff, self.max_cooldown)
        self._state = CircuitState.OPEN
        self._open_until = self._now() + self._cooldown_current
        self.total_stuck_cooldown += self._cooldown_current

    def _clamp_cooldown(self, value: float) -> float:
        return min(max(float(value), _MIN_STATED_COOLDOWN), self.max_cooldown)

    def _close_locked(self) -> None:
        self._state = CircuitState.CLOSED
        self._cooldown_current = self.cooldown
        self._consecutive_throttle = 0
        self._consecutive_transient = 0
        self._probe_slots = 0
        self._probe_successes = 0
        self.total_stuck_cooldown = 0.0
        self._last_retry_after = None

    def _now(self) -> float:
        return self.clock.now() if self.clock is not None else 0.0
