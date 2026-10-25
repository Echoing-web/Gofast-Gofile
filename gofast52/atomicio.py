"""Validated atomic writes for state files (SPEC 4.9 write path).

Single self-contained function. Before anything touches the filesystem:
1. normalize the root with os.path.realpath;
2. derive the destination via os.path.join with a bare literal filename
   (parent components rejected);
3. verify containment of both final and tmp destinations with
   os.path.commonpath.
Then: os.open with explicit write flags -> os.write -> os.fsync -> atomic
os.replace -> best-effort directory fsync.
"""
from __future__ import annotations

import os


def atomic_write_bytes(root: str, filename: str, data: bytes) -> None:
    """Atomically write `data` to root/filename (tmp + fsync + replace)."""
    root_real = os.path.realpath(root)
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError(f"filename must be a bare name: {filename!r}")
    if os.path.pardir in filename.split("/"):
        raise ValueError(f"filename must not reference parent dirs: {filename!r}")
    final = os.path.realpath(os.path.join(root_real, filename))
    if os.path.commonpath([root_real, final]) != root_real:
        raise ValueError(f"destination escapes allowed root: {final}")
    tmp_path = final + ".tmp"
    if os.path.commonpath([root_real, os.path.realpath(tmp_path)]) != root_real:
        raise ValueError(f"tmp destination escapes allowed root: {tmp_path}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, final)
    try:
        dir_fd = os.open(root_real, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # dir fsync best-effort on some platforms; rename is still atomic


def atomic_write_text(root: str, filename: str, text: str) -> None:
    atomic_write_bytes(root, filename, text.encode("utf-8"))


def ensure_sized_file(root: str, filename: str, size: int) -> None:
    """Create root/filename at exactly `size` bytes; extend-only, refuse shrink."""
    if size < 0:
        raise ValueError("size must be >= 0")
    root_real = os.path.realpath(root)
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError(f"filename must be a bare name: {filename!r}")
    if os.path.pardir in filename.split("/"):
        raise ValueError(f"filename must not reference parent dirs: {filename!r}")
    final = os.path.realpath(os.path.join(root_real, filename))
    if os.path.commonpath([root_real, final]) != root_real:
        raise ValueError(f"destination escapes allowed root: {final}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(final, flags)
    try:
        current = os.fstat(fd).st_size
        if current > size:
            raise ValueError(
                f"existing file is larger than target: {current} > {size}")
        if current < size:
            os.ftruncate(fd, size)
    finally:
        os.close(fd)


def fsync_file(root: str, filename: str) -> None:
    """Force an existing file's contents to durable storage (validated path)."""
    root_real = os.path.realpath(root)
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError(f"filename must be a bare name: {filename!r}")
    if os.path.pardir in filename.split("/"):
        raise ValueError(f"filename must not reference parent dirs: {filename!r}")
    final = os.path.realpath(os.path.join(root_real, filename))
    if os.path.commonpath([root_real, final]) != root_real:
        raise ValueError(f"destination escapes allowed root: {final}")
    fd = os.open(final, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_at(root: str, filename: str, offset: int, data: bytes) -> None:
    """Write `data` at absolute `offset` inside root/filename."""
    if offset < 0:
        raise ValueError("offset must be >= 0")
    root_real = os.path.realpath(root)
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError(f"filename must be a bare name: {filename!r}")
    if os.path.pardir in filename.split("/"):
        raise ValueError(f"filename must not reference parent dirs: {filename!r}")
    final = os.path.realpath(os.path.join(root_real, filename))
    if os.path.commonpath([root_real, final]) != root_real:
        raise ValueError(f"destination escapes allowed root: {final}")
    fd = os.open(final, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


class SegmentWriter:
    """One validated open per segment attempt; positioned writes, no fsync.

    v5.3 F2: replaces per-chunk open/seek/write/close with a single open for
    the whole segment fetch (the finalize step fsyncs the file once).
    """

    def __init__(self, root: str, filename: str):
        root_real = os.path.realpath(root)
        if not filename or "/" in filename or "\\" in filename:
            raise ValueError(f"filename must be a bare name: {filename!r}")
        if os.path.pardir in filename.split("/"):
            raise ValueError(f"filename must not reference parent dirs: {filename!r}")
        final = os.path.realpath(os.path.join(root_real, filename))
        if os.path.commonpath([root_real, final]) != root_real:
            raise ValueError(f"destination escapes allowed root: {final}")
        self._path = final
        self._fd = None

    def __enter__(self) -> "SegmentWriter":
        self._fd = os.open(self._path,
                           os.O_RDWR | getattr(os, "O_BINARY", 0))
        return self

    def write(self, offset: int, data: bytes) -> None:
        if self._fd is None:
            raise ValueError("SegmentWriter is not open")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        os.lseek(self._fd, offset, os.SEEK_SET)
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]

    def __exit__(self, *exc) -> bool:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        return False


def rename_into(src_root: str, src_name: str, dst_root: str, dst_name: str) -> None:
    """Atomically rename src_root/src_name to dst_root/dst_name (both validated)."""
    for name in (src_name, dst_name):
        if not name or "/" in name or "\\" in name:
            raise ValueError(f"names must be bare: {name!r}")
        if os.path.pardir in name.split("/"):
            raise ValueError(f"names must not reference parent dirs: {name!r}")
    src_root_real = os.path.realpath(src_root)
    src = os.path.realpath(os.path.join(src_root_real, src_name))
    if os.path.commonpath([src_root_real, src]) != src_root_real:
        raise ValueError(f"source escapes allowed root: {src}")
    dst_root_real = os.path.realpath(dst_root)
    dst = os.path.realpath(os.path.join(dst_root_real, dst_name))
    if os.path.commonpath([dst_root_real, dst]) != dst_root_real:
        raise ValueError(f"destination escapes allowed root: {dst}")
    os.replace(src, dst)
