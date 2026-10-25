"""TDD: gofast52.timeutil contracts (SPEC 4.1)."""
import time

import pytest

from gofast52.timeutil import FakeClock, RealClock, RealSleeper


class TestFakeClock:
    def test_starts_at_given_value(self):
        assert FakeClock(start=42.0).now() == 42.0

    def test_default_start_zero(self):
        assert FakeClock().now() == 0.0

    def test_sleep_advances_now(self):
        c = FakeClock(start=0.0)
        c.sleep(0.5)
        assert c.now() == pytest.approx(0.5)

    def test_sleep_zero_ok(self):
        c = FakeClock(start=7.0)
        c.sleep(0.0)
        assert c.now() == 7.0

    def test_sleep_negative_raises(self):
        with pytest.raises(ValueError):
            FakeClock().sleep(-1)

    def test_many_sleeps_accumulate(self):
        c = FakeClock(start=10.0)
        for _ in range(5):
            c.sleep(1.0)
        assert c.now() == pytest.approx(15.0)


class TestRealClock:
    def test_monotonic_increasing(self):
        c = RealClock()
        a = c.now()
        assert c.now() >= a

    def test_real_sleeper_advances_wall_time(self):
        before = time.monotonic()
        RealSleeper().sleep(0.05)
        assert time.monotonic() - before >= 0.02
