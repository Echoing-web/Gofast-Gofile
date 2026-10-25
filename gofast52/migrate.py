"""v1/v2 -> v5.2 state migration (SPEC 4.15), behavior-compatible with v5.1.

The v2 source directory is treated as strictly read-only; the v5 partial is
created extend-only; parts are copied at their manifest offsets and verified
(size + mtime stable across the copy).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import atomicio
from .errors import FatalError
from .manifest import ManifestData
from .scheduler import Segment

_OLD_PROCESS_NAMES = ["GoFast-GoFile.exe", "gofast.exe", "GoFast.exe"]


def windows_process_running(name_patterns: list[str]) -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"], text=True,
            encoding="mbcs", errors="ignore")
        lower = out.lower()
        return any(p.lower() in lower for p in name_patterns)
    except Exception:
        return False


def migrate_v2(v2_dir: Path, v5_dir: Path | None = None,
               force_name: str | None = None) -> Path:
    if windows_process_running(_OLD_PROCESS_NAMES):
        raise FatalError(
            "检测到旧 GoFast 进程仍在运行。为避免读取正在变化的 .part 文件，"
            "请先让 v2 下载完成或关闭 v2，再执行迁移。原 v2 进程不会被本程序结束。")

    v2_dir = Path(v2_dir).expanduser().resolve()
    if not v2_dir.is_dir():
        raise FileNotFoundError(str(v2_dir))
    manifest_path = v2_dir / "manifest.json"
    if not manifest_path.exists():
        raise FatalError("指定的 v2 工作目录里没有 manifest.json")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        total = int(data["total"])
        target = Path(data["target"]).expanduser().resolve()
    except (ValueError, KeyError, OSError) as e:
        raise FatalError(f"v2 manifest 无法解析: {e}") from e

    name = force_name or target.name
    v5_target = target.parent / name
    v5_state = (Path(v5_dir).expanduser().resolve() if v5_dir
                else v5_target.parent / f".gofast_v5_{v5_target.name}")
    v5_state.mkdir(parents=True, exist_ok=True)
    partial_name = f"{v5_target.name}.partial"

    # extend-only partial creation (refuse larger, keep existing bytes)
    existing = v5_state / partial_name
    if existing.exists() and existing.stat().st_size > total:
        raise FatalError(
            f"existing v5 partial is larger than target: "
            f"{existing.stat().st_size} > {total}")
    atomicio.ensure_sized_file(str(v5_state), partial_name, total)

    raw_segments = data.get("segments") or []
    if not raw_segments:
        raise FatalError("v2 manifest 没有可迁移的 segments")

    segments: list[Segment] = []
    for raw in raw_segments:
        seg = Segment(
            index=int(raw["index"]), start=int(raw["start"]), end=int(raw["end"]),
            retries=int(raw.get("retry", raw.get("retries", 0))))
        src = v2_dir / f"{seg.index:06d}.part"
        if src.exists():
            before = src.stat()
            copy_n = min(before.st_size, seg.total)
            if copy_n:
                offset = 0
                with open(src, "rb") as fin:
                    while offset < copy_n:
                        blob = fin.read(min(16 * 1024 * 1024, copy_n - offset))
                        if not blob:
                            break
                        atomicio.write_at(str(v5_state), partial_name,
                                          seg.start + offset, blob)
                        offset += len(blob)
                after = src.stat()
                if (before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns):
                    raise FatalError(
                        f"迁移期间 v2 分片发生变化: {src.name}。"
                        "已保留 v5 临时文件，但建议删除 v5 state 后重新迁移。")
            seg.done = copy_n
        seg.status = "DONE" if seg.done == seg.total else "WAIT"
        segments.append(seg)

    manifest = ManifestData(
        total=total, target=str(v5_target), workers=16,
        segment_mib=int(data.get("segment_mib", 64)), segments=segments,
        created_by="gofast v5.2 migrate",
    )
    atomicio.atomic_write_text(
        str(v5_state), "manifest.json",
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))

    done_bytes = sum(s.done for s in segments)
    print("\nV2 -> V5.2 迁移已完成。")
    print(f"V2 原目录（未修改）: {v2_dir}")
    print(f"V5 工作目录:        {v5_state}")
    print(f"V5 partial 文件:    {v5_state / partial_name}")
    print(f"已迁移: {done_bytes / 1024 ** 3:.2f} GiB / {total / 1024 ** 3:.2f} GiB")
    print("现在可以用 V5.2 对同一个 GoFile URL 继续下载。")
    print("在确认 V5.2 正常运行前，请不要删除 V2 原目录。")
    return v5_state
