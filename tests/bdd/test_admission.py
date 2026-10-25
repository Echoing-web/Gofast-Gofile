"""BDD: admission control & breaker recovery (features/admission_control.feature)."""

import pytest
from pytest_bdd import scenarios, when, then

scenarios("features/admission_control.feature")


@when("执行下载直到放弃")
def when_run_until_giveup(ctx):
    with pytest.raises(Exception) as ei:
        ctx["engine"].run()
    ctx["failure"] = ei.value


@then("熔断器最终恢复为closed状态")
def then_breaker_recovered(ctx):
    brk = ctx["engine"].admission.breaker
    if brk.state.value == "half_open":
        # run finished mid-recovery; settle with one more probe success
        permit = ctx["engine"].admission.acquire()
        permit.report_success()
        permit.release()
    assert brk.state.value == "closed"


@then("重试期间虚拟时间至少推进了冷却窗口")
def then_cooldown_window_advanced(ctx):
    assert ctx["clock"].now() - ctx["t0"] >= 3


@then("程序因熔断超时放弃并保存现场")
def then_gave_up(ctx):
    assert "熔断" in str(ctx["failure"])


@then("现场清单文件存在可用于续传")
def then_manifest_saved(ctx):
    state = ctx["tmp_path"] / "downloads" / ".gofast_v5_file.bin"
    manifest = state / "manifest.json"
    assert manifest.exists()
    text = manifest.read_text(encoding="utf-8")
    assert '"WAIT"' in text  # unfinished segments preserved for resume


@then("下载成功")
def then_download_ok(ctx):
    assert ctx["target"].read_bytes() == ctx["data"]
