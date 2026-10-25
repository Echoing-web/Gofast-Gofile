"""Recovery scanner (SPEC 4.14): rebuild a manifest from a bare partial file.

Fixes the Recovery-v5.1 bug where the NUL check was written as a 4-character
literal (backslash-x-0-0) instead of a real NUL byte, which marked every
segment DONE regardless of content.

Trust model (review round C, P0-4): writes within a segment are sequential,
so a crashed in-flight segment has real data as a PREFIX and preallocated
zeros as a SUFFIX. The scanner therefore reports the *trusted prefix length*:

- sampled mode: leading non-zero sample windows bracket-prove the gaps before
  them were written; trust ends at the last non-zero byte of the last leading
  non-zero window. The first all-zero sample window stops the count.
- strict mode: full scan, trust ends at the last non-zero byte.

A segment is DONE only when the trusted prefix equals the full size. The
cost of this conservatism is re-downloading legitimately-zero tails; it can
never ship holes (G4/A4).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import atomicio
from .errors import FatalError
from .manifest import ManifestData, ManifestStore
from .scheduler import Segment
from .timeutil import RealClock

SAMPLE_STRIDE = 256 * 1024
SAMPLE_LEN = 16 * 1024
FULL_CHUNK = 1024 * 1024


def _last_nonzero_offset(chunk: bytes) -> int:
    """Offset of the last non-NUL byte in chunk, or -1 when all zero."""
    stripped = chunk.rstrip(b"\x00")
    return len(stripped) - 1


def scan_segment_prefix(fp, start: int, size: int, strict: bool = False) -> int:
    """Return the trusted downloaded-prefix length of [start, start+size).

    Default (sampled): stride windows plus an end-anchored final window.
    Walk leading non-zero windows; stop at the first all-zero one (the write
    frontier was before it). If every window — including the end anchor — is
    non-zero, the segment is treated as complete: the residual risk is a
    crash inside the final <SAMPLE_LEN bytes, which strict mode eliminates.

    strict: full byte scan; trust ends at the last non-NUL byte (maximum
    safety — legitimately-zero tails are re-downloaded).
    """
    if strict:
        fp.seek(start)
        trusted = 0
        remaining = size
        while remaining > 0:
            chunk = fp.read(min(FULL_CHUNK, remaining))
            if not chunk:
                break
            tail = _last_nonzero_offset(chunk)
            if tail >= 0:
                trusted = (size - remaining) + tail + 1
            remaining -= len(chunk)
        return trusted

    offsets = list(range(0, size, SAMPLE_STRIDE))
    if not offsets or offsets[-1] + SAMPLE_LEN < size:
        offsets.append(size - min(SAMPLE_LEN, size))  # end-anchored window
    last_window_off = -1
    last_window = b""
    for off in offsets:
        fp.seek(start + off)
        chunk = fp.read(min(SAMPLE_LEN, size - off))
        if not chunk:
            break
        if chunk.count(b"\x00") == len(chunk):
            break  # first all-zero window: write frontier was before it
        last_window_off = off
        last_window = chunk
    else:
        return size  # every window (incl. the end anchor) carries data
    if last_window_off < 0:
        return 0
    return last_window_off + _last_nonzero_offset(last_window) + 1


def recover_partial(state_dir: Path, segment_mib: int | None = None,
                    strict: bool = False) -> Path:
    """Scan the state dir's .partial and (re)write manifest.json.

    Existing manifest (when parseable) supplies segment size/target hints;
    the partial file itself is the ground truth for totals. The partial file
    is never modified.
    """
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        raise FatalError(f"状态目录不存在: {state_dir}")
    partials = sorted(p for p in state_dir.glob("*.partial"))
    if len(partials) != 1:
        raise FatalError(
            f"状态目录里应恰好有一个 .partial 文件，找到 {len(partials)} 个")
    partial = partials[0]
    name = partial.name[: -len(".partial")]

    seg_mib = segment_mib
    target_hint: str | None = None
    store = ManifestStore(state_dir, RealClock())
    existing = store.load()
    if existing is not None:
        if Path(existing.target).name == name:
            target_hint = existing.target
            if seg_mib is None:
                seg_mib = existing.segment_mib
        if existing.total != partial.stat().st_size:
            print(f"警告: 现有 manifest 的 total ({existing.total}) 与 partial 大小 "
                  f"({partial.stat().st_size}) 不一致，以 partial 为准（清单可能已过期）。")
    if seg_mib is None:
        seg_mib = 64
    seg_size = max(1, seg_mib) * 1024 * 1024
    total = partial.stat().st_size

    print(f"扫描 {partial} ({total / 1024 ** 3:.2f} GiB, 分片 {seg_mib} MiB)"
          + ("，严格全扫模式" if strict else ""))
    segments: list[Segment] = []
    count = (total + seg_size - 1) // seg_size
    with open(partial, "rb") as fp:
        for i in range(count):
            start = i * seg_size
            size = min(seg_size, total - start)
            done = scan_segment_prefix(fp, start, size, strict=strict)
            segments.append(Segment(
                index=i, start=start, end=start + size - 1, done=done,
                status="DONE" if done == size else "WAIT", retries=0))
            if i % 20 == 0:
                print(f"  {i}/{count}")

    manifest = ManifestData(
        total=total,
        target=target_hint or str((partial.parent.parent / name).resolve()),
        workers=8, segment_mib=seg_mib, segments=segments,
        created_by="gofast v5.2 recover",
    )
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
    atomicio.atomic_write_text(str(state_dir), "manifest.json", payload)
    done_bytes = sum(s.done for s in segments)
    print(f"完成: DONE {sum(1 for s in segments if s.status == 'DONE')}/{count} 片, "
          f"可信进度 {done_bytes / 1024 ** 3:.2f} GiB")
    return store.path
