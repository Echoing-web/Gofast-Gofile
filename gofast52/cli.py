"""Command-line interface (SPEC 4.16): v5.1-compatible commands + v5.2 additions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import atomicio
from .engine import DownloadEngine, EngineConfig
from .errors import DownloadError
from .recover import recover_partial

ENGINE_CLS = DownloadEngine  # patchable for tests


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gofast52", description="GoFast-GoFile v5.3 (admission-controlled)")
    sub = ap.add_subparsers(dest="command")

    dl = sub.add_parser("download", help="start/resume a download")
    dl.add_argument("url")
    dl.add_argument("--out", default="downloads")
    dl.add_argument("--workers", type=int, default=16)
    dl.add_argument("--auto-workers", action="store_true",
                    help="v5.1 compat: enable AIMD adaptive concurrency (default on)")
    dl.add_argument("--no-adaptive", dest="adaptive", action="store_false",
                    help="disable AIMD; run fixed at --workers concurrency")
    dl.add_argument("--segment-mib", type=int, default=64)
    dl.add_argument("--retries", type=int, default=8)
    dl.add_argument("--password", default=None)
    dl.add_argument("--token", default=None,
                    help="use your own (premium) account token instead of a guest one")
    dl.add_argument("--req-rate", type=float, default=4.0,
                    help="max new-request rate per second (token bucket)")
    dl.add_argument("--max-open-min", type=float, default=15.0,
                    help="give up after this many minutes of accumulated breaker cooldown")
    dl.add_argument("--verbose", action="store_true",
                    help="print key admission/link events to stderr")

    rec = sub.add_parser("recover", help="rebuild manifest.json by scanning a .partial")
    rec.add_argument("--state-dir", required=True)
    rec.add_argument("--segment-mib", type=int, default=None)
    rec.add_argument("--strict", action="store_true",
                    help="force full-scan verification of every segment")

    mig = sub.add_parser("migrate-v2",
                         help="import v1/v2 .part + manifest into a v5.2 workspace")
    mig.add_argument("--v2-dir", required=True)
    mig.add_argument("--v5-dir", default=None)
    mig.add_argument("--name", default=None)

    rep = sub.add_parser("repair-v5",
                         help="extend a short v5 partial to the expected size; never deletes")
    rep.add_argument("--v5-dir", required=True)
    return ap


def parse(argv: list) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _run_download(args) -> int:
    cfg = EngineConfig(
        url=args.url,
        out=Path(args.out),
        workers=max(1, min(args.workers, 64)),
        segment_mib=max(8, args.segment_mib),
        retries=max(1, args.retries),
        password=args.password,
        token=args.token,
        req_rate=args.req_rate,
        adaptive=args.adaptive,
        auto_workers=True,  # v5.2: AIMD adaptive is the default behavior;
        #                    --auto-workers is kept as a v5.1 compat no-op hint
        max_open_min=args.max_open_min,
        verbose=args.verbose,
    )
    if args.auto_workers:
        print("提示: --auto-workers 已是 v5.2 默认行为（AIMD 自适应并发，"
              "上限为 --workers）。")
    ENGINE_CLS(cfg).run()
    return 0


def _run_repair_v5(v5_dir: str) -> int:
    d = Path(v5_dir).expanduser().resolve()
    mp = d / "manifest.json"
    if not mp.exists():
        print(f"ERROR: 指定的 v5 目录没有 manifest.json: {d}", file=sys.stderr)
        return 1
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
        total = int(data["total"])
        name = Path(data["target"]).name
    except (ValueError, KeyError, OSError) as e:
        print(f"ERROR: manifest 无法解析: {e}", file=sys.stderr)
        return 1
    partial = d / f"{name}.partial"
    if not partial.exists():
        print(f"ERROR: v5 partial 不存在: {partial}", file=sys.stderr)
        return 1
    current = partial.stat().st_size
    if current > total:
        print(f"ERROR: partial 太大，拒绝修改: {current} > {total}", file=sys.stderr)
        return 1
    if current < total:
        atomicio.ensure_sized_file(str(d), f"{name}.partial", total)
    print("V5 partial repair completed.")
    print(f"V5 state:  {d}")
    print(f"Partial:   {partial}")
    print(f"Size:      {partial.stat().st_size} bytes")
    print(f"Expected:  {total} bytes")
    print("现有数据未删除，只将文件扩展到目标大小。")
    return 0


def main(argv: list | None = None) -> int:
    args = parse(argv if argv is not None else sys.argv[1:])
    try:
        if args.command == "download":
            return _run_download(args)
        if args.command == "recover":
            recover_partial(Path(args.state_dir),
                            segment_mib=args.segment_mib, strict=args.strict)
            return 0
        if args.command == "migrate-v2":
            from .migrate import migrate_v2
            migrate_v2(Path(args.v2_dir),
                       Path(args.v5_dir) if args.v5_dir else None, args.name)
            return 0
        if args.command == "repair-v5":
            return _run_repair_v5(args.v5_dir)
    except KeyboardInterrupt:
        print("\n已停止。清单已保存；再次执行相同命令即可继续。", file=sys.stderr)
        return 130
    except DownloadError as e:  # includes FatalError; startup throttles too
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
