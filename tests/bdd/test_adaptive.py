"""BDD: AIMD adaptive concurrency (features/adaptive_concurrency.feature)."""
import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from gofast52.governor import ConcurrencyGovernor
from gofast52.timeutil import FakeClock

scenarios("features/adaptive_concurrency.feature")


@pytest.fixture
def gov():
    return ConcurrencyGovernor(initial=4, maximum=16, minimum=2,
                               success_streak_to_grow=10, grow_step=1,
                               decrease_factor=0.5, grow_cooldown=10.0,
                               clock=FakeClock(start=1000.0))


@given("一个初始并发为4上限为16下限为2的调速器", target_fixture="gov")
def given_governor(gov):
    return gov


@when(parsers.parse("连续{n:d}次成功回报"))
def when_successes(gov, n):
    for _ in range(n):
        gov.on_success()


@when(parsers.parse("再连续{n:d}次成功回报"))
def when_more_successes(gov, n):
    for _ in range(n):
        gov.on_success()


@when(parsers.parse("{n:d}次限流回报"))
def when_throttle(gov, n):
    for _ in range(n):
        gov.on_throttled()


@when(parsers.parse("再{n:d}次限流回报"))
def when_more_throttle(gov, n):
    for _ in range(n):
        gov.on_throttled()


@when("1次限流回报后立刻50次成功回报")
def when_throttle_then_successes(gov):
    gov.on_throttled()
    for _ in range(50):
        gov.on_success()


@when("虚拟时间前进11秒后再10次成功回报")
def when_advance_then_successes(gov):
    gov.clock.sleep(11.0)
    for _ in range(10):
        gov.on_success()


@then(parsers.parse("当前并发为{limit:d}"))
def then_limit(gov, limit):
    assert gov.limit == limit


@then(parsers.parse("当前并发仍为{limit:d}"))
def then_limit_still(gov, limit):
    assert gov.limit == limit
