"""BDD: v5.3 speed & resilience (features/v53_speed.feature)."""
import json

from pytest_bdd import scenarios, given, when, then

from gofast52.api import TokenCache
from gofast52.engine import DownloadEngine, EngineConfig
from gofast52.timeutil import FakeClock
from tests.test_engine import ScriptedServer, allow_all_guard

scenarios("features/v53_speed.feature")

MIB = 1024 * 1024


@given("一个带大尾段分片的可续传状态")
def given_big_tail_state(ctx):
    data = bytes(range(256)) * 4096 * 7  # 7 MiB
    ctx["data"] = data
    clock = FakeClock(start=1_700_000_000.0)
    server = ScriptedServer(data)
    state = ctx["tmp_path"] / "downloads" / ".gofast_v5_file.bin"
    state.mkdir(parents=True)
    (state / "file.bin.partial").write_bytes(
        data[:2 * MIB] + b"\x00" * (len(data) - 2 * MIB))
    (state / "manifest.json").write_text(json.dumps({
        "version": 52, "total": len(data),
        "target": str((ctx["tmp_path"] / "downloads" / "file.bin").resolve()),
        "workers": 4, "segment_mib": 64,
        "segments": [
            {"index": 0, "start": 0, "end": MIB - 1, "done": MIB,
             "status": "DONE", "retries": 0},
            {"index": 1, "start": MIB, "end": 2 * MIB - 1, "done": MIB,
             "status": "DONE", "retries": 0},
            {"index": 2, "start": 2 * MIB, "end": len(data) - 1, "done": 0,
             "status": "WAIT", "retries": 0},
        ],
    }), encoding="utf-8")
    cfg = EngineConfig(url="https://gofile.io/d/abc123",
                       out=ctx["tmp_path"] / "downloads", workers=4,
                       segment_mib=64, req_rate=1000.0, split_min_mib=1)
    eng = DownloadEngine(cfg, transport=server, clock=clock, sleeper=clock,
                         ui=False, guard=allow_all_guard(),
                         token_cache=TokenCache(ctx["tmp_path"] / "tok.json"))
    ctx.update(clock=clock, server=server, engine=eng, t0=clock.now())


@when("用4个工作线程执行下载")
def when_run_with_four_workers(ctx):
    ctx["target"] = ctx["engine"].run()


@then("下载完成且内容正确")
def then_completed_correct(ctx):
    assert ctx["target"].read_bytes() == ctx["data"]


@then("尾段被至少3个并行请求覆盖")
def then_tail_split_covered(ctx):
    spans = [(a, b) for (_, a, b) in ctx["server"].range_requests]
    tail = [s for s in spans if s[0] >= 2 * MIB]
    assert len(tail) >= 3, spans


@then("熔断累计冷却远小于15分钟预算")
def then_much_less_than_budget(ctx):
    # deterministic metric (same one the watchdog uses): 10 x Retry-After=2
    # must schedule ~20s of cooldown in total, not the old escalating
    # 60/90/135/... pattern that breached the 900s budget
    breaker = ctx["engine"].admission.breaker
    assert breaker.total_stuck_cooldown < 900.0
    assert breaker.state.value == "closed"
