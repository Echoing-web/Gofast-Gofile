"""BDD: v5.1 backward compatibility (features/compatibility.feature)."""
import json

from pytest_bdd import scenarios, given, when, then

from gofast52 import cli
from gofast52.manifest import ManifestData, ManifestStore
from gofast52.scheduler import Segment
from gofast52.timeutil import FakeClock

scenarios("features/compatibility.feature")

MIB = 1024 * 1024


@given("一个v5.1格式的清单记录了1个已完成分片")
def given_v51_manifest(ctx):
    data = ctx["data"]
    state = ctx["tmp_path"] / "downloads" / ".gofast_v5_file.bin"
    state.mkdir(parents=True)
    (state / "file.bin.partial").write_bytes(
        data[:MIB] + b"\x00" * (len(data) - MIB))
    (state / "manifest.json").write_text(json.dumps({
        "version": 5, "total": len(data),
        "target": str((ctx["tmp_path"] / "downloads" / "file.bin").resolve()),
        "workers": 16, "segment_mib": 1,
        "segments": [
            {"index": 0, "start": 0, "end": MIB - 1, "done": MIB,
             "status": "DONE", "retries": 0},
            {"index": 1, "start": MIB, "end": 2 * MIB - 1, "done": 0,
             "status": "WAIT", "retries": 0},
            {"index": 2, "start": 2 * MIB, "end": len(data) - 1, "done": 0,
             "status": "WAIT", "retries": 0},
        ],
    }), encoding="utf-8")


@when("用v5.2执行下载")
def when_run_v52(ctx):
    ctx["target"] = ctx["engine"].run()


@then("下载完成且内容正确")
def then_completed(ctx):
    assert ctx["target"].read_bytes() == ctx["data"]


@when("解析v5.1风格的download命令行")
def when_parse_v51_cli(ctx):
    ctx["args"] = cli.parse([
        "download", "https://gofile.io/d/abc",
        "--workers", "32", "--auto-workers", "--segment-mib", "128",
    ])


@then("workers为32且auto_workers为真且segment_mib为128")
def then_args_parsed(ctx):
    a = ctx["args"]
    assert a.workers == 32
    assert a.auto_workers is True
    assert a.segment_mib == 128


@given("一份v5.2保存的清单")
def given_v52_manifest(ctx):
    store = ManifestStore(ctx["tmp_path"] / "state", FakeClock())
    store.save(ManifestData(
        total=1000, target=str(ctx["tmp_path"] / "f.bin"), workers=8,
        segment_mib=64, segments=[Segment(0, 0, 499, 500, "DONE", 0),
                                  Segment(1, 500, 999, 0, "WAIT", 0)]), force=True)
    ctx["manifest_file"] = store.path


@then("其JSON结构可被v5.1加载逻辑还原为分片列表")
def then_v51_can_read(ctx):
    raw = json.loads(ctx["manifest_file"].read_text(encoding="utf-8"))
    assert raw["total"] == 1000
    assert isinstance(raw["target"], str)
    segs = [Segment(**x) for x in raw["segments"]]  # v5.1 loads segments this way
    assert segs[0].done == 500
    assert segs[0].total == 500
