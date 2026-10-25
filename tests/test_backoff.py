"""TDD: gofast52.backoff contracts (SPEC 4.3)."""
import pytest

from gofast52.backoff import BackoffPolicy


class StubRng:
    """Deterministic rng: random() always returns `value`."""

    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


def make(jitter="full", rng_value=0.0, **kw):
    return BackoffPolicy(
        base=kw.pop("base", 1.0), factor=kw.pop("factor", 2.0),
        cap=kw.pop("cap", 60.0), jitter=jitter,
        rng=StubRng(rng_value), **kw,
    )


class TestUpperBound:
    def test_sequence_without_jitter(self):
        p = make(jitter="none")
        assert [p.delay(i) for i in range(6)] == [1, 2, 4, 8, 16, 32]

    def test_capped_at_cap(self):
        p = make(jitter="none")
        assert p.delay(7) == 60  # 1*2^7=128 -> cap 60
        assert p.delay(30) == 60

    def test_attempt_zero_min_positive(self):
        assert make(jitter="none").delay(0) == 1.0


class TestFullJitter:
    def test_jitter_zero_gives_zero(self):
        p = make(rng_value=0.0)
        assert p.delay(3) == 0.0

    def test_jitter_one_gives_upper_bound(self):
        p = make(rng_value=0.999999)
        assert p.delay(3) == pytest.approx(8.0, rel=1e-3)

    def test_never_exceeds_upper_bound(self):
        for attempt in range(5):
            p = make(rng_value=1.0)
            ub = make(jitter="none").delay(attempt)
            assert p.delay(attempt) <= ub + 1e-9

    def test_default_jitter_is_full(self):
        # default constructor uses full jitter and must not raise without rng
        p = BackoffPolicy()
        assert 0.0 <= p.delay(0) <= 1.0


class TestRetryAfter:
    def test_retry_after_dominates_when_larger(self):
        p = make(rng_value=0.0)
        d = p.delay(0, retry_after=30.0)
        assert 30.0 <= d <= 31.0  # ra + uniform(0,1)

    def test_retry_after_floor_up_to_jitter_one(self):
        p = make(rng_value=0.999)
        d = p.delay(0, retry_after=30.0)
        assert 30.0 <= d < 31.0

    def test_retry_after_capped_at_1800(self):
        p = make(rng_value=0.999)
        assert p.delay(0, retry_after=999_999.0) <= 1800.0

    def test_upper_bound_still_applies_when_ra_missing(self):
        p = make(rng_value=0.5)
        assert p.delay(2, retry_after=None) == pytest.approx(2.0)


class TestInvariants:
    def test_delay_never_negative(self):
        p = make(rng_value=0.0)
        for a in range(10):
            for ra in (None, 0.0, 5.0):
                assert p.delay(a, retry_after=ra) >= 0

    def test_upper_bound_monotonic(self):
        p = make(jitter="none")
        seq = [p.delay(i) for i in range(12)]
        assert seq == sorted(seq)
