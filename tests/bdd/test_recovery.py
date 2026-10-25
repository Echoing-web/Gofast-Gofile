"""BDD: recovery scan (features/recovery_scan.feature)."""
import json

from pytest_bdd import scenarios, given, when, then

scenarios("features/recovery_scan.feature")

MIB = 1024 * 1024


def _make_state(ctx, segments):
    state = ctx["tmp_path"] / ".gofast_v5_file.bin"
    state.mkdir(parents=True)
    (state / "file.bin.partial").write_bytes(b"".join(segments))
    ctx["state"] = state
    ctx["partial_before"] = (state / "file.bin.partial").read_bytes()


@given("一个3分片的partial文件其中第2片全零")
def given_middle_zero(ctx):
    _make_state(ctx, [b"\x11" * MIB, b"\x00" * MIB, b"\x22" * MIB])


@given("一个3分片的partial文件其中第3片只在采样窗口之间有数据")
def given_island_between_samples(ctx):
    # 300KiB of zeros, an 8-byte island, then zeros to 1MiB: the island sits
    # outside every sampled 16KiB window start at 0K/256K/512K/768K
    seg2 = b"\x00" * (300 * 1024) + b"\x42" * 8 + b"\x00" * (MIB - 300 * 1024 - 8)
    _make_state(ctx, [b"\x11" * MIB, b"\x22" * MIB, seg2])


@when("执行恢复扫描")
def when_scan(ctx):
    from gofast52.recover import recover_partial
    ctx["manifest_path"] = recover_partial(ctx["state"], segment_mib=1)


@then("清单标记为 DONE WAIT DONE")
def then_statuses(ctx):
    m = json.loads(ctx["manifest_path"].read_text(encoding="utf-8"))
    assert [s["status"] for s in m["segments"]] == ["DONE", "WAIT", "DONE"]


@then("partial文件内容未被修改")
def then_partial_untouched(ctx):
    path = ctx["state"] / "file.bin.partial"
    assert path.read_bytes() == ctx["partial_before"]


@then("第3片被保守判定为WAIT（宁重下不交付空洞）")
def then_third_done(ctx):
    m = json.loads(ctx["manifest_path"].read_text(encoding="utf-8"))
    assert m["segments"][2]["status"] == "WAIT"
    assert m["segments"][2]["done"] == 0
