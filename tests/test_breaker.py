"""TDD: gofast52.breaker contracts (SPEC 4.5).

All timing uses FakeClock: before_request sleeps virtually.
"""
import pytest

from gofast52.breaker import CircuitBreaker, CircuitState


@pytest.fixture
def br(clock):
    return CircuitBreaker(throttle_threshold=5, transient_threshold=8,
                          cooldown=60.0, probes=2, clock=clock)


class TestClosed:
    def test_starts_closed(self, br):
        assert br.state is CircuitState.CLOSED

    def test_below_threshold_stays_closed(self, br):
        for _ in range(4):
            br.on_throttled(None)
        assert br.state is CircuitState.CLOSED

    def test_success_resets_consecutive_counter(self, br):
        for _ in range(4):
            br.on_throttled(None)
        br.on_success()
        for _ in range(4):
            br.on_throttled(None)
        assert br.state is CircuitState.CLOSED  # counter restarted


class TestOpen:
    def test_five_consecutive_throttles_open(self, br, clock):
        for _ in range(5):
            br.on_throttled(None)
        assert br.state is CircuitState.OPEN
        assert br.open_until == pytest.approx(clock.now() + 60.0)

    def test_retry_after_extends_cooldown(self, br, clock):
        for _ in range(4):
            br.on_throttled(None)
        br.on_throttled(retry_after=120.0)
        assert br.state is CircuitState.OPEN
        assert br.open_until == pytest.approx(clock.now() + 120.0)

    def test_transient_threshold_opens(self, br):
        for _ in range(8):
            br.on_transient()
        assert br.state is CircuitState.OPEN


class TestHalfOpen:
    def _open_it(self, br, clock):
        for _ in range(5):
            br.on_throttled(None)
        br.before_request(sleeper=clock)  # sleeps through cooldown
        assert br.state is CircuitState.HALF_OPEN

    def test_open_blocks_until_cooldown(self, br, clock):
        for _ in range(5):
            br.on_throttled(None)
        br.before_request(sleeper=clock)
        assert clock.now() == pytest.approx(1000.0 + 60.0)

    def test_half_open_admits_exactly_probes(self, br, clock):
        # _open_it consumes probe slot 1 while sleeping through cooldown
        self._open_it(br, clock)
        t = clock.now()
        br.before_request(sleeper=clock)  # probe slot 2 — the last one
        assert clock.now() == t  # both admitted immediately
        # a 3rd caller must wait (no probe slots); verify via a cancellable thread
        import threading
        from gofast52.errors import OperationCancelled
        cancel = threading.Event()
        holder = {}

        def third():
            try:
                br.before_request(sleeper=clock, cancel=cancel)
                holder["done"] = True
            except OperationCancelled:
                holder["cancelled"] = True

        threads = [threading.Thread(target=third, daemon=True)]
        for _t in threads:
            _t.start()
        _t.join(timeout=5)
        assert _t.is_alive()  # still blocked -> no free slot
        assert clock.now() > t  # it has been polling virtual time
        cancel.set()
        _t.join(timeout=5)
        assert not _t.is_alive()
        assert holder.get("cancelled") is True

    def test_before_request_cancel_during_open(self, br, clock):
        import threading
        from gofast52.errors import OperationCancelled
        for _ in range(5):
            br.on_throttled(None)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(OperationCancelled):
            br.before_request(sleeper=clock, cancel=cancel)

    def test_all_probes_success_closes(self, br, clock):
        # _open_it consumes probe slot 1; one more call takes slot 2 (last)
        self._open_it(br, clock)
        br.before_request(sleeper=clock)  # probe slot 2 — the last one
        br.on_success()                   # probe 1 success
        assert br.state is CircuitState.HALF_OPEN  # needs both probes
        br.on_success()                   # probe 2 success -> close
        assert br.state is CircuitState.CLOSED

    def test_probe_failure_reopens_with_backoff(self, br, clock):
        self._open_it(br, clock)
        br.before_request(sleeper=clock)
        br.on_throttled(None)
        assert br.state is CircuitState.OPEN
        assert br.open_until == pytest.approx(clock.now() + 90.0)  # 60*1.5

    def test_cooldown_backoff_capped(self, clock):
        br = CircuitBreaker(throttle_threshold=1, cooldown=60.0,
                            probes=1, max_cooldown=600.0, clock=clock)
        reopen_at = 60.0
        for cycle in range(12):
            br.on_throttled(None)          # opens (threshold=1)
            reopen_at = min(60.0 * (1.5 ** cycle), 600.0)
            br.before_request(sleeper=clock)  # sleep to half-open
            br.on_throttled(None)          # probe fails -> reopen
            assert br.open_until <= clock.now() + 600.0 + 1e-9
        assert reopen_at == 600.0


class TestMixed:
    def test_transients_reset_by_success(self, br):
        for _ in range(7):
            br.on_transient()
        br.on_success()
        for _ in range(7):
            br.on_transient()
        assert br.state is CircuitState.CLOSED

    def test_open_until_none_when_closed(self, br):
        assert br.open_until is None


class TestRetryAfterAwareEscalation:
    """v5.3: a stated Retry-After wins over blind escalation — a "wait 2s"
    answer must not turn into minutes of cooldown (stress-storm finding)."""

    def test_small_retry_after_does_not_escalate_on_reopen(self, clock):
        br = CircuitBreaker(throttle_threshold=1, cooldown=60.0, probes=1,
                            max_cooldown=600.0, clock=clock)
        br.on_throttled(retry_after=2.0)          # OPEN with stated 2s
        assert br.open_until == pytest.approx(clock.now() + 2.0)
        for cycle in range(5):                     # probes keep saying 2s
            br.before_request(sleeper=clock)       # consume the probe slot
            br.on_throttled(retry_after=2.0)       # reopen
            assert br.open_until == pytest.approx(clock.now() + 2.0)  # no growth
        assert br.total_stuck_cooldown == pytest.approx(2.0 * 6)

    def test_no_retry_after_still_escalates(self, clock):
        br = CircuitBreaker(throttle_threshold=1, cooldown=60.0, probes=1,
                            max_cooldown=600.0, clock=clock)
        br.on_throttled(None)
        assert br.open_until == pytest.approx(clock.now() + 60.0)
        br.before_request(sleeper=clock)
        br.on_throttled(None)                       # reopen without a signal
        assert br.open_until == pytest.approx(clock.now() + 90.0)  # x1.5

    def test_huge_retry_after_capped_at_max_cooldown(self, clock):
        br = CircuitBreaker(throttle_threshold=1, cooldown=60.0, probes=1,
                            max_cooldown=600.0, clock=clock)
        br.on_throttled(retry_after=99999.0)
        assert br.open_until == pytest.approx(clock.now() + 600.0)


class TestProbeSlotLifecycle:
    """Review round C: probe slots must not leak on non-congestion outcomes."""

    def _half_open(self, clock, probes=1, threshold=1):
        br = CircuitBreaker(throttle_threshold=threshold, cooldown=60.0,
                            probes=probes, clock=clock)
        br.on_throttled(None)          # OPEN (threshold=1)
        br.before_request(sleeper=clock)  # consumes the only probe slot
        assert br.state is CircuitState.HALF_OPEN
        return br

    def test_abandoned_probe_returns_slot(self, clock):
        br = self._half_open(clock)
        br.on_abandoned()              # e.g. the request died with LinkExpired
        br.before_request(sleeper=clock)  # slot available again -> admits
        br.on_success()
        assert br.state is CircuitState.CLOSED  # probes=1: one success closes

    def test_stale_success_not_counted_as_probe(self, clock):
        # P2-13: a request that predates the OPEN completes during HALF_OPEN;
        # its success must not count toward probe successes
        br = CircuitBreaker(throttle_threshold=1, cooldown=60.0,
                            probes=2, clock=clock)
        br.on_throttled(None)
        br.before_request(sleeper=clock)  # probe slot 1
        br.on_success(is_probe=False)     # stale in-flight success: ignored
        assert br.state is CircuitState.HALF_OPEN
        br.on_success()                   # real probe 1 success
        assert br.state is CircuitState.HALF_OPEN  # needs both probes
        br.on_abandoned()                 # probe 2 abandoned -> slot back
        br.before_request(sleeper=clock)  # probe 2 (re-taken)
        br.on_success()
        assert br.state is CircuitState.CLOSED
