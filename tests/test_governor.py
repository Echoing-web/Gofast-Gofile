"""TDD: gofast52.governor contracts (SPEC 4.6, defaults per review round R3)."""
import pytest

from gofast52.governor import ConcurrencyGovernor


@pytest.fixture
def gov(clock):
    return ConcurrencyGovernor(initial=4, maximum=16, minimum=2,
                               success_streak_to_grow=10, grow_step=1,
                               decrease_factor=0.5, grow_cooldown=10.0,
                               clock=clock)


class TestBounds:
    def test_starts_at_initial(self, gov):
        assert gov.limit == 4

    def test_limit_never_exceeds_maximum(self, gov, clock):
        for _ in range(500):
            gov.on_success()
            clock.sleep(0.01)
        assert gov.limit == 16

    def test_limit_never_below_minimum(self, gov):
        for _ in range(20):
            gov.on_throttled()
        assert gov.limit == 2


class TestAdditiveIncrease:
    def test_growth_needs_streak(self, gov):
        for _ in range(9):
            gov.on_success()
        assert gov.limit == 4  # streak 9 < 10
        gov.on_success()
        assert gov.limit == 5

    def test_streak_resets_after_growth(self, gov):
        for _ in range(10):
            gov.on_success()
        assert gov.limit == 5
        for _ in range(9):
            gov.on_success()
        assert gov.limit == 5
        gov.on_success()
        assert gov.limit == 6

    def test_throttle_resets_streak(self, gov):
        for _ in range(9):
            gov.on_success()
        gov.on_throttled()
        for _ in range(9):
            gov.on_success()
        assert gov.limit == 2  # 4 -> 2 by throttle; streak restarted, no growth

    def test_growth_blocked_during_cooldown(self, gov, clock):
        gov.on_throttled()                     # decrease at t=1000
        clock.sleep(5.0)                       # within cooldown(10s)
        for _ in range(30):
            gov.on_success()
        assert gov.limit == 2                  # no growth inside cooldown
        clock.sleep(6.0)                       # past cooldown
        for _ in range(10):
            gov.on_success()
        assert gov.limit == 3


class TestMultiplicativeDecrease:
    def test_throttle_halves(self, gov):
        gov.on_throttled()
        assert gov.limit == 2  # ceil(4*0.5)

    def test_repeated_throttle_floors(self, gov):
        gov.on_throttled()
        gov.on_throttled()
        assert gov.limit == 2

    def test_transient_minus_one(self, gov):
        gov.on_transient()
        assert gov.limit == 3
        for _ in range(10):
            gov.on_transient()
        assert gov.limit == 2

    def test_decrease_records_time(self, gov, clock):
        gov.on_throttled()
        assert gov.last_decrease == pytest.approx(clock.now())


class TestNonDefaultShape:
    def test_custom_shape(self, clock):
        g = ConcurrencyGovernor(initial=2, maximum=4, minimum=1,
                                success_streak_to_grow=2, grow_step=2,
                                decrease_factor=0.5, grow_cooldown=0.0,
                                clock=clock)
        g.on_success(); g.on_success()
        assert g.limit == 4          # +2 capped at max
        g.on_throttled()
        assert g.limit == 2          # ceil(4*0.5)
        g.on_throttled()
        assert g.limit == 1          # floor
