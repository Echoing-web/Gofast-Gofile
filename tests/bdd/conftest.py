"""Shared BDD steps and fixtures (engine-level scenarios)."""
from __future__ import annotations

import pytest
from pytest_bdd import given, when, then, parsers

from gofast52.api import TokenCache
from gofast52.engine import DownloadEngine, EngineConfig
from gofast52.timeutil import FakeClock
from tests.test_engine import ScriptedServer, allow_all_guard
from tests.test_transport import FakeResponse

MIB = 1024 * 1024
THREE_SEGMENT_DATA = bytes(range(256)) * 4096 * 3  # 3 MiB -> 3 x 1MiB segments


class BddCtx(dict):
    pass


@pytest.fixture
def ctx(tmp_path) -> BddCtx:
    return BddCtx(tmp_path=tmp_path)


def _ensure_engine(ctx: BddCtx, **cfg_over) -> DownloadEngine:
    if "engine" in ctx:
        return ctx["engine"]
    clock = FakeClock(start=1_700_000_000.0)
    server = ScriptedServer(ctx["data"])
    cfg = EngineConfig(
        url="https://gofile.io/d/abc123",
        out=ctx["tmp_path"] / "downloads",
        workers=4, segment_mib=1, req_rate=1000.0, **cfg_over)
    eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                         ui=False, guard=allow_all_guard(),
                         token_cache=TokenCache(ctx["tmp_path"] / "tok.json"))
    ctx.update(clock=clock, server=server, engine=eng, t0=clock.now())
    return eng


# --------------------------------------------------------------------- shared
@given("一个3分片的可下载文件")
def given_three_segment_file(ctx):
    ctx["data"] = THREE_SEGMENT_DATA
    _ensure_engine(ctx)


@given(parsers.parse("服务器连续{n:d}次返回429并要求等待{wait:d}秒"))
def given_throttle_storm(ctx, n, wait):
    for _ in range(n):
        ctx["server"].queue("range", FakeResponse(429, {"Retry-After": str(wait)}))


@given(parsers.parse("服务器先返回{n:d}次429并要求等待{wait:d}秒"))
def given_throttle_first(ctx, n, wait):
    _ensure_engine(ctx)
    for _ in range(n):
        ctx["server"].queue("range", FakeResponse(429, {"Retry-After": str(wait)}))


@when("执行下载")
def when_run_download(ctx):
    ctx["target"] = ctx["engine"].run()


@then("下载最终成功且文件内容完整")
def then_completed_intact(ctx):
    assert ctx["target"].read_bytes() == ctx["data"]


@then(parsers.parse("虚拟时间推进不少于{seconds:d}秒"))
def then_virtual_time_advanced(ctx, seconds):
    assert ctx["clock"].now() - ctx["t0"] >= seconds
