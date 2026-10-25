"""TDD: gofast52.ratelimit contracts (SPEC 4.4)."""
import threading

import pytest

from gofast52.ratelimit import TokenBucket


@pytest.fixture
def bucket(clock):
    # rate=2/s, burst=4, prefilled with 4 tokens at start
    return TokenBucket(rate=2.0, burst=4, clock=clock)


class TestBurst:
    def test_first_burst_acquires_free(self, bucket, clock):
        for _ in range(4):
            bucket.acquire(sleeper=clock)
        assert clock.now() == pytest.approx(1000.0)  # no time passed

    def test_fifth_acquire_waits_one_token(self, bucket, clock):
        for _ in range(4):
            bucket.acquire(sleeper=clock)
        bucket.acquire(sleeper=clock)
        assert clock.now() == pytest.approx(1000.5)  # 1 token / (2/s) = 0.5s


class TestRefill:
    def test_refill_over_elapsed_time(self, clock):
        b = TokenBucket(rate=4.0, burst=4, clock=clock)
        for _ in range(4):
            b.acquire(sleeper=clock)
        clock.sleep(0.5)  # accrues 2 tokens
        b.acquire(sleeper=clock)
        b.acquire(sleeper=clock)
        assert clock.now() == pytest.approx(1000.5)

    def test_refill_capped_at_burst(self, clock):
        b = TokenBucket(rate=4.0, burst=4, clock=clock)
        clock.sleep(100.0)  # long idle
        for _ in range(4):
            b.acquire(sleeper=clock)
        assert clock.now() == pytest.approx(1100.0)  # all free, no more than burst


class TestMultiToken:
    def test_acquire_two_tokens(self, clock):
        b = TokenBucket(rate=2.0, burst=4, clock=clock)
        b.acquire(tokens=2, sleeper=clock)
        assert clock.now() == pytest.approx(1000.0)
        b.acquire(tokens=3, sleeper=clock)  # needs 1 more token -> 0.5s
        assert clock.now() == pytest.approx(1000.5)


class TestThreadSafety:
    def test_concurrent_acquires_never_oversell(self, clock):
        b = TokenBucket(rate=10.0, burst=10, clock=clock)
        granted = []
        lock = threading.Lock()

        def worker():
            for _ in range(5):
                b.acquire(sleeper=clock)
                with lock:
                    granted.append(clock.now())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 40 requests at rate 10/s must take >= 3.0s of virtual time
        assert clock.now() >= 1000.0 + 3.0 - 0.01
        assert len(granted) == 40
