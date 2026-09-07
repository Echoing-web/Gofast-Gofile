from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Lock, Event
from typing import Optional
from urllib.parse import quote, urlparse

import requests

API_BASE = "https://api.gofile.io"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
DEFAULT_SALT = "12af056dacea0b"
DEFAULT_WORKERS = 16
DEFAULT_SEGMENT_MIB = 64
DEFAULT_RETRIES = 8
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 60
UI_INTERVAL = 0.5


def website_token(account_token: str, salt: str) -> str:
    slot = int(time.time()) // 14400
    raw = f"{UA}::en-US::{account_token}::{slot}::{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
        "Origin": "https://gofile.io",
        "Referer": "https://gofile.io/",
    })
    return s


def direct_headers(token: str, range_value: Optional[str] = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": "https://gofile.io/",
        "Cookie": f"accountToken={token}",
    }
    if range_value:
        h["Range"] = range_value
    return h


def content_id(url: str) -> str:
    p = urlparse(url)
    m = re.search(r"/d/([A-Za-z0-9_-]+)", p.path)
    if not m:
        raise ValueError("无法识别 GoFile 链接，请使用 https://gofile.io/d/XXXXXX")
    return m.group(1)


def create_account(s: requests.Session, salt: str) -> str:
    r = s.post(
        f"{API_BASE}/accounts",
        headers={"X-Website-Token": website_token("", salt), "X-BL": "en-US"},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "ok":
        raise RuntimeError(f"GoFile 账号创建失败: {j}")
    return j["data"]["token"]


def get_content(s: requests.Session, cid: str, token: str, salt: str, password: Optional[str]) -> dict:
    url = f"{API_BASE}/contents/{quote(cid)}?cache=true&sortField=createTime&sortDirection=1"
    if password:
        url += "&password=" + quote(password)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Website-Token": website_token(token, salt),
        "X-BL": "en-US",
    }
    last = None
    for attempt in range(DEFAULT_RETRIES):
        try:
            r = s.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
            j = r.json()
            if j.get("status") != "ok":
                raise RuntimeError(f"GoFile API 返回错误: {j}")
            return j["data"]
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"无法取得 GoFile 文件信息: {last}")


def flatten(data: dict) -> list[dict]:
    if data.get("type") == "file":
        return [data]
    out = []
    for child in data.get("children", {}).values():
        if child.get("type") == "folder":
            out.extend(flatten(child))
        else:
            out.append(child)
    return out


@dataclass
class Segment:
    index: int
    start: int
    end: int
    done: int = 0
    status: str = "WAIT"
    retries: int = 0

    @property
    def total(self) -> int:
        return self.end - self.start + 1


class GoFastV5:
    def __init__(self, args):
        self.url = args.url
        self.out = Path(args.out).expanduser().resolve()
        self.requested_workers = max(1, min(args.workers, 64))
        self.segment_mib = max(8, args.segment_mib)
        self.retries = max(1, args.retries)
        self.password = args.password
        self.auto_workers = args.auto_workers
        self.salt = os.environ.get("GOFILE_WT_SALT", DEFAULT_SALT)
        self.s = new_session()
        self.token = ""
        self.lock = Lock()
        self.samples: list[tuple[float, int]] = []

    def resolve(self) -> dict:
        self.token = create_account(self.s, self.salt)
        data = get_content(self.s, content_id(self.url), self.token, self.salt, self.password)
        files = flatten(data)
        if not files:
            raise RuntimeError("没有找到文件")
        if len(files) > 1:
            print("\n发现多个文件：")
            for i, f in enumerate(files, 1):
                print(f"  [{i}] {f.get('name', '未知文件')}  {int(f.get('size', 0))/1024**3:.2f} GiB")
            while True:
                try:
                    n = int(input("请选择文件编号: "))
                    if 1 <= n <= len(files):
                        return files[n - 1]
                except ValueError:
                    pass
        return files[0]

    def prepare(self, info: dict) -> tuple[str, int, Path]:
        url = info.get("link") or info.get("directLink")
        if not url:
            raise RuntimeError("GoFile 返回结果缺少文件直链")
        total = int(info.get("size") or 0)
        target = self.out / Path(info.get("name") or "download.bin").name
        self.out.mkdir(parents=True, exist_ok=True)
        if total <= 0:
            with self.s.get(
                url, headers=direct_headers(self.token, "bytes=0-0"),
                stream=True, allow_redirects=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            ) as r:
                cr = r.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)$", cr)
                total = int(m.group(1)) if m else int(r.headers.get("Content-Length", 0))
        if total <= 0:
            raise RuntimeError("无法确定文件大小")
        return url, total, target

    def supports_range(self, url: str) -> bool:
        with self.s.get(
            url, headers=direct_headers(self.token, "bytes=0-0"),
            stream=True, allow_redirects=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        ) as r:
            return r.status_code == 206 and r.headers.get("Content-Range", "").startswith("bytes 0-0/")

    def auto_select_workers(self, url: str, total: int) -> int:
        candidates = [4, 8, 16, 24, 32, 48, 64]
        if total < 256 * 1024 * 1024:
            candidates = [4, 8, 16, 32]
        probe = 2 * 1024 * 1024
        print("\n自动并发测速（少量 Range 探测，不写目标文件）")
        results = []
        for n in candidates:
            t0 = time.monotonic()
            futures = []
            try:
                with ThreadPoolExecutor(max_workers=n) as ex:
                    for i in range(n):
                        start = (i * probe) % max(total - probe, 1)
                        end = min(start + probe - 1, total - 1)
                        futures.append(ex.submit(self._probe, url, start, end))
                    for f in as_completed(futures):
                        f.result()
                speed = n * probe / max(time.monotonic() - t0, 0.001)
                results.append((speed, n))
                print(f"  并发 {n:2d} -> {speed/1024**2:7.1f} MiB/s")
            except Exception as e:
                print(f"  并发 {n:2d} -> 失败（{e}）")
                results.append((0.0, n))
        speed, best = max(results)
        print(f"选择并发 {best}（{speed/1024**2:.1f} MiB/s）")
        return best

    def _probe(self, url: str, start: int, end: int) -> None:
        with self.s.get(
            url, headers=direct_headers(self.token, f"bytes={start}-{end}"),
            stream=True, allow_redirects=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        ) as r:
            cr = r.headers.get("Content-Range", "")
            if r.status_code != 206 or not cr.startswith(f"bytes {start}-{end}/"):
                raise RuntimeError(f"HTTP {r.status_code}, Content-Range={cr or '<none>'}")
            for _ in r.iter_content(1024 * 1024):
                pass

    def make_segments(self, total: int) -> list[Segment]:
        size = self.segment_mib * 1024 * 1024
        return [Segment(i, p, min(total - 1, p + size - 1))
                for i, p in enumerate(range(0, total, size))]

    def state_dir(self, target: Path) -> Path:
        d = target.parent / f".gofast_v5_{target.name}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def load_manifest(self, d: Path, target: Path, total: int) -> list[Segment]:
        mp = d / "manifest.json"
        if mp.exists():
            try:
                j = json.loads(mp.read_text(encoding="utf-8"))
                if j.get("total") == total and j.get("target") == str(target):
                    return [Segment(**x) for x in j["segments"]]
            except Exception:
                pass
        return self.make_segments(total)

    def save_manifest(self, d: Path, target: Path, total: int, workers: int, segs: list[Segment]):
        tmp = d / "manifest.json.tmp"
        (d / "manifest.json.tmp").write_text(
            json.dumps({
                "version": 5,
                "total": total,
                "target": str(target),
                "workers": workers,
                "segment_mib": self.segment_mib,
                "segments": [asdict(x) for x in segs]
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, d / "manifest.json")

    def create_partial(self, path: Path, total: int):
        # 兼容之前迁移或下载留下的临时文件，保留已有字节并拒绝状态错配。
        if path.exists():
            size = path.stat().st_size
            if size > total:
                raise RuntimeError(f"v5 partial 文件过大: {size} > {total}")
            if size < total:
                with open(path, "r+b") as f:
                    f.truncate(total)
            return
        with open(path, "wb") as f:
            f.truncate(total)

    def run(self):
        info = self.resolve()
        url, total, target = self.prepare(info)
        print(f"文件: {target}")
        print(f"大小: {total/1024**3:.2f} GiB")
        if not self.supports_range(url):
            print("当前节点没有提供 206 Partial Content，将使用单连接断点续传。")
            self.single(url, total, target)
            return

        workers = self.auto_select_workers(url, total) if self.auto_workers else self.requested_workers
        state = self.state_dir(target)
        partial = state / f"{target.name}.partial"
        segs = self.load_manifest(state, target, total)
        self.create_partial(partial, total)

        print(f"并发: {workers} | 分片: {self.segment_mib} MiB")
        print(f"临时文件: {partial}")
        self.download_ranges(url, target, total, state, partial, segs, workers)

    def download_ranges(self, url, target, total, state, partial, segs, workers):
        pending = [s for s in segs if s.done < s.total]
        start_time = time.monotonic()
        stop_ui = Event()

        import threading

        def ui_loop():
            while not stop_ui.is_set():
                self.render(target, total, workers, segs, start_time)
                time.sleep(UI_INTERVAL)

        ui = threading.Thread(target=ui_loop, daemon=True)
        ui.start()

        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(self.download_segment, url, partial, s): s
                    for s in pending
                }
                for f in as_completed(futures):
                    f.result()
                    with self.lock:
                        self.save_manifest(state, target, total, workers, segs)
        except KeyboardInterrupt:
            with self.lock:
                self.save_manifest(state, target, total, workers, segs)
            raise
        finally:
            stop_ui.set()
            ui.join(timeout=2)

        if any(s.done != s.total for s in segs):
            self.save_manifest(state, target, total, workers, segs)
            raise RuntimeError("部分 Range 尚未完成，现场已保存。")

        # 不再合并分片，直接将完整的临时文件改为最终文件。
        if target.exists():
            backup = target.with_suffix(target.suffix + ".old")
            if backup.exists():
                backup.unlink()
            os.replace(target, backup)
        os.replace(partial, target)
        try:
            (state / "manifest.json").unlink()
            state.rmdir()
        except OSError:
            pass
        print(f"\n完成: {target}")

    def download_segment(self, url: str, partial: Path, seg: Segment) -> int:
        for attempt in range(self.retries):
            try:
                with self.lock:
                    seg.status = "DOWN"
                    seg.retries = attempt
                start = seg.start + seg.done
                if start > seg.end:
                    with self.lock:
                        seg.done = seg.total
                        seg.status = "DONE"
                    return seg.total

                with self.s.get(
                    url, headers=direct_headers(self.token, f"bytes={start}-{seg.end}"),
                    stream=True, allow_redirects=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                ) as r:
                    cr = r.headers.get("Content-Range", "")
                    if r.status_code != 206 or not cr.startswith(f"bytes {start}-{seg.end}/"):
                        raise RuntimeError(f"HTTP {r.status_code}, Content-Range={cr or '<none>'}")

                    with open(partial, "r+b", buffering=4 * 1024 * 1024) as f:
                        f.seek(start)
                        for chunk in r.iter_content(1024 * 1024):
                            if chunk:
                                f.write(chunk)
                                with self.lock:
                                    seg.done = min(seg.done + len(chunk), seg.total)

                if seg.done != seg.total:
                    raise RuntimeError(f"分片未完成: {seg.done}/{seg.total}")

                with self.lock:
                    seg.status = "DONE"
                return seg.done
            except Exception:
                with self.lock:
                    seg.status = "RETRY" if attempt + 1 < self.retries else "FAIL"
                if attempt + 1 == self.retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
        return 0

    def render(self, target: Path, total: int, workers: int, segs: list[Segment], started: float):
        with self.lock:
            done = sum(s.done for s in segs)
            active = sum(s.status == "DOWN" for s in segs)
            retry = sum(s.status == "RETRY" for s in segs)
            fail = sum(s.status == "FAIL" for s in segs)
            count = len(segs)
        now = time.monotonic()
        self.samples.append((now, done))
        self.samples = [(t, b) for t, b in self.samples if t >= now - 8]
        if len(self.samples) > 1:
            dt = self.samples[-1][0] - self.samples[0][0]
            speed = (self.samples[-1][1] - self.samples[0][1]) / max(dt, 0.001)
        else:
            speed = 0.0
        avg = done / max(now - started, 0.001)
        eta = (total - done) / speed if speed > 0 else None
        eta_s = "--:--:--" if eta is None else f"{int(eta)//3600:02d}:{(int(eta)%3600)//60:02d}:{int(eta)%60:02d}"
        pct = done / total * 100
        fill = int(32 * pct / 100)
        bar = "█" * fill + "░" * (32 - fill)
        sys.stdout.write(
            "\033[2J\033[H"
            f"{target.name}\n"
            f"[{bar}] {pct:6.2f}%\n"
            f"已下载      {done/1024**3:8.2f} / {total/1024**3:.2f} GiB\n"
            f"当前速度    {speed/1024**2:8.1f} MiB/s    平均 {avg/1024**2:8.1f} MiB/s\n"
            f"预计剩余    {eta_s}               并发 {workers}   活跃 {active}\n"
            f"分片状态    完成 {sum(s.status=='DONE' for s in segs):4d}/{count:<4d} "
            f"重试 {retry:3d}  失败 {fail:3d}\n"
        )
        sys.stdout.flush()

    def single(self, url: str, total: int, target: Path):
        part = target.with_suffix(target.suffix + ".part")
        existing = part.stat().st_size if part.exists() else 0
        while existing < total:
            hdr = direct_headers(self.token, f"bytes={existing}-") if existing else direct_headers(self.token)
            with self.s.get(url, headers=hdr, stream=True, allow_redirects=True,
                            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
                if existing and r.status_code != 206:
                    raise RuntimeError(f"服务器未接受断点续传，HTTP {r.status_code}")
                with open(part, "ab" if existing else "wb", buffering=4 * 1024 * 1024) as f:
                    for chunk in r.iter_content(4 * 1024 * 1024):
                        if chunk:
                            f.write(chunk)
            existing = part.stat().st_size
        os.replace(part, target)
        print(f"完成: {target}")


def windows_process_running(name_patterns: list[str]) -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True,
            encoding="mbcs",
            errors="ignore",
        )
        lower = out.lower()
        return any(p.lower() in lower for p in name_patterns)
    except Exception:
        return False


def migrate_v2(v2_dir: Path, v5_dir: Path, force_name: Optional[str] = None):
    """
    安全迁移：
    - 检测到已知 GoFast v1/v2 进程时拒绝迁移；
    - 读取 v2 清单；
    - 创建一个预分配的 v5 临时文件；
    - 将已有 .part 文件复制到对应偏移；
    - 检查复制期间分片是否发生变化；
    - 写入可继续下载的 v5 清单。
    原 v2 目录不会被修改或删除。
    """
    if windows_process_running(["GoFast-GoFile.exe", "gofast.exe", "GoFast.exe"]):
        raise RuntimeError(
            "检测到旧 GoFast 进程仍在运行。为避免读取正在变化的 .part 文件，"
            "请先让 v2 下载完成或关闭 v2，再执行迁移。原 v2 进程不会被本程序结束。"
        )

    v2_dir = v2_dir.expanduser().resolve()
    if not v2_dir.is_dir():
        raise FileNotFoundError(v2_dir)
    manifest = v2_dir / "manifest.json"
    if not manifest.exists():
        raise RuntimeError("指定的 v2 工作目录里没有 manifest.json")

    data = json.loads(manifest.read_text(encoding="utf-8"))
    total = int(data["total"])
    target = Path(data["target"]).expanduser().resolve()
    name = force_name or target.name
    # 默认 v5 状态目录放在 v2 目标文件旁，并使用 v5 专用目录名。
    v5_target = target.parent / name
    v5_state = (v5_dir.expanduser().resolve() if v5_dir else (v5_target.parent / f".gofast_v5_{v5_target.name}"))
    v5_state.mkdir(parents=True, exist_ok=True)
    partial = v5_state / f"{v5_target.name}.partial"

    # 只创建一个 v5 临时文件；已有迁移留下的短文件只扩展，不删除已复制字节。
    if partial.exists():
        current = partial.stat().st_size
        if current > total:
            raise RuntimeError(f"现有 v5 临时文件大于目标大小: {current} > {total}")
        if current < total:
            with open(partial, "r+b") as f:
                f.truncate(total)
    else:
        with open(partial, "wb") as f:
            f.truncate(total)

    segs = []
    for raw in data.get("segments", []):
        seg = Segment(
            int(raw["index"]), int(raw["start"]), int(raw["end"]),
            0, "WAIT", int(raw.get("retry", raw.get("retries", 0)))
        )
        src = v2_dir / f"{seg.index:06d}.part"
        if src.exists():
            before = src.stat()
            expected = seg.total
            copy_n = min(before.st_size, expected)
            if copy_n:
                with open(src, "rb") as fin, open(partial, "r+b", buffering=4 * 1024 * 1024) as fout:
                    fout.seek(seg.start)
                    remaining = copy_n
                    while remaining:
                        b = fin.read(min(16 * 1024 * 1024, remaining))
                        if not b:
                            break
                        fout.write(b)
                        remaining -= len(b)
                after = src.stat()
                if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                    raise RuntimeError(
                        f"迁移期间 v2 分片发生变化: {src.name}。"
                        "已保留 v5 临时文件，但建议删除 v5 state 后重新迁移。"
                    )
                seg.done = copy_n
        seg.status = "DONE" if seg.done == seg.total else "WAIT"
        segs.append(seg)

    # 旧清单没有可用分片信息时，无法安全推断偏移，因此直接停止。
    if not segs:
        raise RuntimeError("v2 manifest 没有可迁移的 segments")

    out_manifest = v5_state / "manifest.json"
    out_manifest.write_text(json.dumps({
        "version": 5,
        "migrated_from": str(v2_dir),
        "total": total,
        "target": str(v5_target),
        "workers": 16,
        "segment_mib": data.get("segment_mib", 64),
        "segments": [asdict(x) for x in segs],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nV2 → V5 迁移已完成。")
    print(f"V2 原目录（未修改）: {v2_dir}")
    print(f"V5 工作目录:        {v5_state}")
    print(f"V5 partial 文件:    {partial}")
    print(f"已迁移: {sum(s.done for s in segs)/1024**3:.2f} GiB / {total/1024**3:.2f} GiB")
    print("现在可以用 V5 对同一个 GoFile URL 继续下载。")
    print("在确认 V5 正常运行前，请不要删除 V2 原目录。")


def repair_v5(v5_dir: Path):
    v5_dir = v5_dir.expanduser().resolve()
    mp = v5_dir / "manifest.json"
    if not mp.exists():
        raise RuntimeError("指定的 v5 目录没有 manifest.json")
    data = json.loads(mp.read_text(encoding="utf-8"))
    total = int(data["total"])
    target = Path(data["target"]).expanduser().resolve()
    partial = v5_dir / f"{target.name}.partial"
    if not partial.exists():
        raise RuntimeError(f"v5 partial 不存在: {partial}")
    current = partial.stat().st_size
    if current > total:
        raise RuntimeError(f"partial 太大，拒绝修改: {current} > {total}")
    if current < total:
        with open(partial, "r+b") as f:
            f.truncate(total)
    print("V5 临时文件修复已完成。")
    print(f"V5 状态目录: {v5_dir}")
    print(f"临时文件:     {partial}")
    print(f"当前大小:     {partial.stat().st_size} bytes")
    print(f"目标大小:     {total} bytes")
    print("现有数据未删除，只将文件扩展到目标大小。")


def cmd_migrate(args):
    migrate_v2(Path(args.v2_dir), Path(args.v5_dir) if args.v5_dir else None, args.name)


def main() -> int:
    ap = argparse.ArgumentParser(description="GoFast-GoFile v5 下载工具")
    sub = ap.add_subparsers(dest="command")

    dl = sub.add_parser("download", help="开始或继续下载")
    dl.add_argument("url")
    dl.add_argument("--out", default="downloads")
    dl.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    dl.add_argument("--auto-workers", action="store_true")
    dl.add_argument("--segment-mib", type=int, default=DEFAULT_SEGMENT_MIB)
    dl.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    dl.add_argument("--password", default=None)

    mig = sub.add_parser("migrate-v2", help="将 v1/v2 分片和清单导入 v5 状态")
    mig.add_argument("--v2-dir", required=True, help="包含 manifest.json 和 *.part 的 v2 目录")
    mig.add_argument("--v5-dir", default=None, help="可选的 v5 状态目录")
    mig.add_argument("--name", default=None, help="可选的最终文件名")

    rep = sub.add_parser("repair-v5", help="将过短的 v5 临时文件扩展到目标大小，不删除已有字节")
    rep.add_argument("--v5-dir", required=True, help="包含 manifest.json 和 *.partial 的 v5 目录")

    args = ap.parse_args()

    if args.command == "migrate-v2":
        try:
            cmd_migrate(args)
            return 0
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    if args.command == "repair-v5":
        try:
            repair_v5(Path(args.v5_dir))
            return 0
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    if args.command != "download":
        ap.print_help()
        return 2

    try:
        GoFastV5(args).run()
        return 0
    except KeyboardInterrupt:
        print("\n已停止。v5 manifest 已保存；再次执行相同命令即可继续。")
        return 130
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
