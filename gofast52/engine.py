"""Download orchestrator (SPEC 4.13).

Wires admission control, dynamic segment scheduling, crash-safe manifests,
link refresh and the single-connection fallback around an injected transport.
Every outbound request goes through the AdmissionController; every outcome is
classified and fed back to the breaker/AIMD governor.
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import atomicio
from .admission import AdmissionController, run_guarded
from .api import UA, GoFileApi, TokenCache, flatten
from .backoff import BackoffPolicy
from .breaker import CircuitBreaker
from .errors import (FatalError, LinkExpired, OperationCancelled, Throttled,
                     TransientError)
from .governor import ConcurrencyGovernor
from .manifest import ManifestData, ManifestStore
from .ratelimit import TokenBucket
from .scheduler import Segment, SegmentScheduler
from .ssrf import URLGuard
from .timeutil import Clock, FakeClock, RealClock, RealSleeper, Sleeper
from .transport import RequestsTransport

CHUNK_SIZE = 4 * 1024 * 1024  # v5.3: 4 MiB chunks (fewer syscalls/GIL switches)
_LINK_REFRESH_MIN_INTERVAL = 30.0
_LINK_REFRESH_MAX = 5
_CONTENT_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")


@dataclass
class EngineConfig:
    url: str
    out: Path = Path("downloads")
    workers: int = 16
    segment_mib: int = 64
    retries: int = 8
    password: Optional[str] = None
    token: Optional[str] = None
    req_rate: float = 4.0
    adaptive: bool = True
    max_open_min: float = 15.0
    auto_workers: bool = True  # v5.1 compat flag; adaptive mode is the default
    split_min_mib: float = 8.0  # v5.3: tail-split floor for starved workers
    connect_timeout: float = 20.0
    read_timeout: float = 60.0
    verbose: bool = False


def content_id(url: str) -> str:
    from urllib.parse import urlparse
    m = _CONTENT_ID_RE.search(urlparse(url).path)
    if not m:
        raise FatalError("无法识别 GoFile 链接，请使用 https://gofile.io/d/XXXXXX")
    return m.group(1)


class _LinkRefresher:
    """Direct-link refresh with cross-worker coordination.

    When several workers hit an expired link at once, the first refreshes and
    the others wait for the engine's link to change instead of failing the
    whole run (review P1-6). The refreshed link is matched to the selected
    file by id/name/size so multi-file contents never fetch the wrong file's
    link (review P1-8).
    """

    def __init__(self, api: GoFileApi, cid: str, token: str, password, clock: Clock,
                 sleeper: Sleeper, get_link: Callable[[], str], want: dict):
        self.api = api
        self.cid = cid
        self.token = token
        self.password = password
        self.clock = clock
        self.sleeper = sleeper
        self.get_link = get_link
        self.want = want  # the file entry the user selected
        self.count = 0
        self.last_refresh: Optional[float] = None
        self._lock = threading.Lock()

    def refresh(self, stale_link: str) -> str:
        with self._lock:
            windowed = (self.last_refresh is not None
                        and self.clock.now() - self.last_refresh < _LINK_REFRESH_MIN_INTERVAL)
        if windowed:
            return self._wait_for_new_link(stale_link)
        if self.count >= _LINK_REFRESH_MAX:
            raise FatalError("直链刷新次数耗尽，放弃")
        data = self.api.get_content(self.cid, self.token, self.password)
        link = self._match_link(flatten(data))
        if not link:
            raise FatalError("刷新后仍未找到所选文件的直链")
        with self._lock:
            self.count += 1
            self.last_refresh = self.clock.now()
        return link

    def _wait_for_new_link(self, stale_link: str) -> str:
        deadline = (self.last_refresh or self.clock.now()) + _LINK_REFRESH_MIN_INTERVAL + 5.0
        while self.clock.now() < deadline:
            current = self.get_link()
            if current != stale_link:
                return current  # another worker already refreshed
            self.sleeper.sleep(0.25)
        raise FatalError("等待其他线程刷新直链超时")

    def _match_link(self, files: list[dict]) -> Optional[str]:
        want_id = self.want.get("id")
        want_name = self.want.get("name")
        want_size = self.want.get("size")
        for f in files:
            link = f.get("link") or f.get("directLink")
            if not link:
                continue
            f_id = f.get("id")
            if want_id and f_id:
                if f_id == want_id:
                    return link
                continue
            if f.get("name") == want_name and f.get("size") == want_size:
                return link
        return None


class DownloadEngine:
    def __init__(
        self,
        config: EngineConfig,
        transport=None,
        clock: Optional[Clock] = None,
        sleeper: Optional[Sleeper] = None,
        ui: bool = True,
        choose_file: Optional[Callable[[list], dict]] = None,
        guard: Optional[URLGuard] = None,
        token_cache: Optional[TokenCache] = None,
    ):
        self.cfg = config
        self.clock = clock if clock is not None else RealClock()
        if sleeper is not None:
            self.sleeper = sleeper
        elif isinstance(self.clock, FakeClock):
            self.sleeper = self.clock
        else:
            self.sleeper = RealSleeper()
        self.transport = transport or RequestsTransport(
            pool_size=max(4, config.workers))
        self.guard = guard if guard is not None else URLGuard()
        self.token_cache = token_cache if token_cache is not None else TokenCache(
            Path.home() / ".gofast52" / "guest_token.json")
        self.choose_file = choose_file
        self.ui_enabled = ui

        workers = max(1, min(config.workers, 64))
        adaptive = config.adaptive and config.auto_workers
        if adaptive:
            initial = max(1, min(4, workers))
            minimum = max(1, min(2, initial))
            governor = ConcurrencyGovernor(
                initial=initial, maximum=workers, minimum=minimum,
                success_streak_to_grow=10, grow_step=1,
                decrease_factor=0.5, grow_cooldown=10.0, clock=self.clock)
        else:
            governor = ConcurrencyGovernor(
                initial=workers, maximum=workers, minimum=workers, clock=self.clock)
        self.admission = AdmissionController(
            bucket=TokenBucket(rate=max(0.5, config.req_rate), burst=workers,
                               clock=self.clock),
            breaker=CircuitBreaker(
                throttle_threshold=5, transient_threshold=8,
                cooldown=60.0, probes=2, clock=self.clock),
            governor=governor,
            clock=self.clock, sleeper=self.sleeper,
        )
        self.api = GoFileApi(transport=self.transport, admission=self.admission,
                             now_fn=self.clock.now, token_cache=self.token_cache)
        self.backoff = BackoffPolicy(base=1.0, factor=2.0, cap=60.0, jitter="full")

        self._cancel = threading.Event()
        self._done = threading.Event()  # tells watchdog/aux threads the run ended
        self._fatal_reason: Optional[BaseException] = None
        self._fatal_lock = threading.Lock()
        self._link: Optional[str] = None
        self._link_lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def run(self) -> Path:
        try:
            return self._run_inner()
        except KeyboardInterrupt:
            self._cancel.set()
            self._save_state(force=True)
            raise
        except FatalError:
            self._cancel.set()
            self._save_state(force=True)
            raise

    # ------------------------------------------------------------------ phases
    def _run_inner(self) -> Path:
        token, data = self.api.get_content_auto(
            content_id(self.cfg.url), self.cfg.password, explicit_token=self.cfg.token)
        files = flatten(data)
        if not files:
            raise FatalError("没有找到文件")
        info = files[0] if len(files) == 1 else self._pick(files)
        link = info.get("link") or info.get("directLink")
        if not link:
            raise FatalError("GoFile 返回结果缺少文件直链")
        self.guard.validate(link)
        self._link = link
        cid = content_id(self.cfg.url)
        refresher = _LinkRefresher(
            self.api, cid, token, self.cfg.password, self.clock, self.sleeper,
            self.current_link, info)

        total = int(info.get("size") or 0)
        supports_range, probed_total = self._probe_range(token)
        if total <= 0:
            total = probed_total
        if total <= 0:
            raise FatalError("无法确定文件大小")

        target = Path(self.cfg.out).expanduser() / Path(info.get("name") or "download.bin").name
        if not supports_range:
            print("当前节点没有提供 206 Partial Content，将使用单连接断点续传。")
            return self._download_single(target, total, token)

        state_dir = target.parent / f".gofast_v5_{target.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        store = ManifestStore(state_dir, self.clock)
        manifest = store.load()
        if (manifest is None or manifest.total != total
                or Path(manifest.target).resolve() != target.resolve()):
            manifest = ManifestData(
                total=total, target=str(target), workers=self.cfg.workers,
                segment_mib=self.cfg.segment_mib,
                segments=self._make_segments(total))
        store.save(manifest, force=True)

        partial_name = f"{target.name}.partial"
        try:
            atomicio.ensure_sized_file(str(state_dir), partial_name, total)
        except ValueError as e:
            raise FatalError(f"partial 文件大小异常: {e}") from e

        scheduler = SegmentScheduler(manifest.segments, max_retries=self.cfg.retries)
        self._manifest = manifest
        self._store = store
        self._state_dir = state_dir
        self._partial_name = partial_name
        self._target = target

        self._run_workers(scheduler, refresher, token)

        incomplete = [s for s in scheduler.snapshot() if s.status != "DONE"]
        if incomplete:
            self._save_state(force=True)
            raise FatalError(
                f"部分 Range 尚未完成（{len(incomplete)} 片），现场已保存。")

        self._finalize(target, state_dir, partial_name)
        return target

    # ------------------------------------------------------------- components
    def _pick(self, files: list) -> dict:
        if self.choose_file is not None:
            return self.choose_file(files)
        print("\n发现多个文件：")
        for i, f in enumerate(files, 1):
            size = int(f.get("size", 0))
            print(f"  [{i}] {f.get('name','unknown')}  {size/1024**3:.2f} GiB")
        while True:
            try:
                n = int(input("请选择文件编号: "))
                if 1 <= n <= len(files):
                    return files[n - 1]
            except ValueError:
                pass

    def _probe_range(self, token: str):
        """One admitted request with Range 0-0; returns (supports, total)."""
        def probe():
            with self.transport.stream(
                self.current_link(),
                headers=self._direct_headers(token, "bytes=0-0"),
                timeout=(self.cfg.connect_timeout, self.cfg.read_timeout),
                expect_range=False,
            ) as r:
                cr = r.headers.get("Content-Range", "")
                if r.status_code == 206 and cr.startswith("bytes 0-0/"):
                    m = re.search(r"/(\d+)$", cr)
                    return True, (int(m.group(1)) if m else 0)
                cl = r.headers.get("Content-Length", "")
                return False, (int(cl) if cl.isdigit() else 0)
        return self._retry_transient(probe, budget=3)

    def _retry_transient(self, fn, budget: int = 3):
        last: Optional[BaseException] = None
        for attempt in range(budget):
            try:
                return run_guarded(self.admission, fn, cancel=self._cancel)
            except Throttled as e:
                last = e
                self.sleeper.sleep(self.backoff.delay(attempt, e.retry_after))
            except TransientError as e:
                last = e
                self.sleeper.sleep(self.backoff.delay(attempt))
        raise FatalError(f"探测失败，已重试 {budget} 次: {last}")

    def _make_segments(self, total: int) -> list[Segment]:
        # no artificial floor here: tests use tiny segments; the CLI enforces
        # the documented 8 MiB minimum for interactive use
        size = max(1, self.cfg.segment_mib) * 1024 * 1024
        return [Segment(i, p, min(total - 1, p + size - 1))
                for i, p in enumerate(range(0, total, size))]

    def _direct_headers(self, token: Optional[str], range_value: Optional[str]) -> dict:
        h = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Referer": "https://gofile.io/",
        }
        if token:
            h["Cookie"] = f"accountToken={token}"
        if range_value:
            h["Range"] = range_value
        return h

    def current_link(self) -> str:
        with self._link_lock:
            if not self._link:
                raise FatalError("直链未初始化")
            return self._link

    def _set_link(self, link: str) -> None:
        self.guard.validate(link)
        with self._link_lock:
            changed = self._link != link
            self._link = link
        if changed:
            self._log("direct link refreshed")

    # -------------------------------------------------------------- worker pool
    def _run_workers(self, scheduler: SegmentScheduler, refresher, token: str):
        self._scheduler_for_save = scheduler
        pending = sum(1 for s in scheduler.snapshot() if s.status != "DONE")
        if pending == 0:
            return
        # spawn the full worker pool even when pending < workers: idle workers
        # trigger tail-splitting instead of exiting (v5.3 F1), and exit fast
        # when nothing is claimable
        n_threads = max(1, self.cfg.workers)
        watchdog = threading.Thread(target=self._watchdog, daemon=True)
        watchdog.start()
        renderer = None
        render_stop = threading.Event()
        if self.ui_enabled:
            renderer = threading.Thread(
                target=self._render_loop, args=(scheduler, render_stop), daemon=True)
            renderer.start()
        try:
            errors: "queue.Queue[BaseException]" = queue.Queue()
            threads = []
            for _ in range(n_threads):
                t = threading.Thread(
                    target=self._worker, args=(scheduler, refresher, token, errors),
                    daemon=True)
                t.start()
                threads.append(t)
            try:
                while any(t.is_alive() for t in threads):
                    for t in threads:
                        t.join(timeout=0.2)
            except KeyboardInterrupt:
                self._cancel.set()
                deadline = time.monotonic() + 5.0  # total drain budget (D12)
                while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
                    for t in threads:
                        t.join(timeout=0.1)
                raise
        finally:
            self._done.set()
            render_stop.set()
            watchdog.join(timeout=2)
            if renderer is not None:
                renderer.join(timeout=2)
        if not errors.empty():
            raise errors.get()
        if self._fatal_reason is not None:
            raise self._fatal_reason

    def _worker(self, scheduler: SegmentScheduler, refresher, token, errors):
        min_split_bytes = max(1, int(self.cfg.split_min_mib * 1024 * 1024))
        while not self._cancel.is_set():
            seg = scheduler.claim()
            if seg is None:
                # starved: try to split a large remaining segment so this
                # worker (and others) get work instead of exiting (v5.3 F1)
                if not scheduler.exhausted() and scheduler.maybe_split(
                        min_split_bytes, self.cfg.workers):
                    seg = scheduler.claim()
                if seg is None:
                    return
            try:
                self._process_segment(seg, scheduler, refresher, token)
            except (FatalError, Throttled, TransientError) as e:
                self._set_fatal(e)
                self._cancel.set()
                errors.put(e)
                return
            except OperationCancelled:
                return
            except Exception as e:  # unclassified (e.g. disk full): abort loudly
                # instead of dying silently and leaving the segment DOWN forever
                err = FatalError(f"worker 未分类异常: {e!r}")
                self._set_fatal(err)
                self._cancel.set()
                errors.put(err)
                return

    def _process_segment(self, seg: Segment, scheduler, refresher, token):
        while True:
            if self._cancel.is_set():
                raise OperationCancelled("cancelled")
            start = seg.start + seg.done
            if start > seg.end:
                scheduler.complete(seg)
                self._save_state()
                return
            try:
                self._fetch_segment(seg, start, token)
                scheduler.complete(seg)
                self._save_state()
                return
            except LinkExpired:
                try:
                    self._set_link(refresher.refresh(self.current_link()))
                except Throttled as e:
                    # refresh hit API rate limiting: global congestion signal,
                    # back off and retry via the claim loop (link stays stale)
                    scheduler.requeue(seg, count_retry=False)
                    self.sleeper.sleep(self.backoff.delay(0, e.retry_after))
                    return
                except TransientError:
                    scheduler.requeue(seg, count_retry=False)
                    self.sleeper.sleep(self.backoff.delay(seg.retries))
                    return
                continue  # retry the same segment with the fresh link
            except Throttled as e:
                # throttling is a global congestion signal handled by the
                # breaker/watchdog; it must not burn the segment retry budget
                scheduler.requeue(seg, count_retry=False)
                self.sleeper.sleep(self.backoff.delay(seg.retries, e.retry_after))
                return  # back to claim loop (segment returns to index order)
            except TransientError:
                scheduler.requeue(seg)
                self.sleeper.sleep(self.backoff.delay(seg.retries))
                return

    def _fetch_segment(self, seg: Segment, start: int, token: str):
        headers = self._direct_headers(token, f"bytes={start}-{seg.end}")
        pos = start

        def body():
            with self.transport.stream(
                self.current_link(), headers=headers,
                timeout=(self.cfg.connect_timeout, self.cfg.read_timeout),
            ) as r:
                for chunk in r.iter_content(CHUNK_SIZE):
                    if chunk:
                        yield chunk

        permit = self.admission.acquire(cancel=self._cancel)
        try:
            with atomicio.SegmentWriter(str(self._state_dir),
                                        self._partial_name) as writer:
                for chunk in body():
                    if pos + len(chunk) > seg.end + 1:
                        raise TransientError(
                            f"server sent more bytes than requested for segment "
                            f"{seg.index} (pos={pos}, chunk={len(chunk)}, end={seg.end})")
                    writer.write(pos, chunk)
                    pos += len(chunk)
                    seg.done = min(seg.done + len(chunk), seg.total)
            if seg.done != seg.total:
                raise TransientError(f"segment incomplete {seg.done}/{seg.total}")
            permit.report_success()
        except Throttled as e:
            permit.report_throttled(e.retry_after)
            raise
        except TransientError:
            permit.report_transient()
            raise
        except (FatalError, LinkExpired, OperationCancelled):
            permit.release()
            raise
        finally:
            permit.release()

    # -------------------------------------------------------------- watchdog
    def _watchdog(self):
        """Abort the run when the breaker's accumulated stuck-cooldown budget
        is exceeded (G7 unattended safety).

        total_stuck_cooldown sums every open/reopen cooldown since the last
        CLOSED state — in production this equals the wall time the server has
        been refusing us — and is immune to clock-burn from waiting pollers.
        """
        max_stuck = max(1.0, self.cfg.max_open_min * 60.0)
        while not (self._cancel.is_set() or self._done.is_set()):
            if self.admission.breaker.total_stuck_cooldown > max_stuck:
                self._set_fatal(FatalError(
                    f"熔断器累计冷却超过 {self.cfg.max_open_min:.1f} 分钟仍未恢复，"
                    "已保存现场并放弃（G7 无人值守安全）。"))
                self._cancel.set()
                return
            self.sleeper.sleep(0.25)

    # -------------------------------------------------------------- finalize
    def _save_state(self, force: bool = False):
        store = getattr(self, "_store", None)
        manifest = getattr(self, "_manifest", None)
        if store is None or manifest is None:
            return
        scheduler = getattr(self, "_scheduler_for_save", None)
        if scheduler is not None:
            manifest.segments = scheduler.snapshot()
        store.save(manifest, force=force)

    def _finalize(self, target: Path, state_dir: Path, partial_name: str):
        # SPEC 4.13(6): force the partial's data to disk BEFORE the rename
        atomicio.fsync_file(str(state_dir), partial_name)
        if target.exists():
            backup = target.with_name(target.name + ".old")
            try:
                backup.unlink()
            except OSError:
                pass
            target.replace(backup)
        atomicio.rename_into(str(state_dir), partial_name,
                             str(target.parent), target.name)
        try:
            (state_dir / "manifest.json").unlink()
            state_dir.rmdir()
        except OSError:
            pass
        print(f"\n完成: {target}")

    # --------------------------------------------------------- single stream
    def _download_single(self, target: Path, total: int, token: str) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        part_name = target.name + ".part"
        part = target.parent / part_name
        # append-mode progress: the .part file is NEVER preallocated, so its
        # size is exactly the verified prefix (v5.1 semantics; review P0-2)
        existing = part.stat().st_size if part.exists() else 0
        if existing > total:
            raise FatalError(f"续传文件过大: {existing} > {total}")
        if not part.exists():
            atomicio.ensure_sized_file(str(target.parent), part_name, 0)
        attempt = 0
        while existing < total and not self._cancel.is_set():
            rng = f"bytes={existing}-" if existing else None
            headers = self._direct_headers(token, rng)
            try:
                written = run_guarded(
                    self.admission,
                    lambda: self._stream_to_file(
                        str(target.parent), part_name, existing, headers,
                        expect_range=bool(rng)),
                    cancel=self._cancel)
                existing += written
                attempt = 0
            except Throttled as e:
                self.sleeper.sleep(self.backoff.delay(attempt, e.retry_after))
                attempt += 1
            except TransientError as e:
                if existing > 0 and ("ignored Range" in str(e) or "resume rejected" in str(e)):
                    raise FatalError(
                        f"服务器不支持断点续传（对 Range 请求返回完整内容），"
                        f"无法从 {existing} 字节处续传") from e
                self.sleeper.sleep(self.backoff.delay(attempt))
                attempt += 1
            if attempt >= max(3, self.cfg.retries):
                raise FatalError("单连接模式重试耗尽")
        atomicio.fsync_file(str(target.parent), part_name)
        atomicio.rename_into(str(target.parent), part_name,
                             str(target.parent), target.name)
        print(f"\n完成: {target}")
        return target

    def _stream_to_file(self, root: str, name: str, offset: int, headers,
                        expect_range=False):
        pos = offset
        limit_left = None  # clamp: never write beyond what the request covers

        def body():
            nonlocal limit_left
            with self.transport.stream(
                self.current_link(), headers=headers,
                timeout=(self.cfg.connect_timeout, self.cfg.read_timeout),
                expect_range=expect_range,
            ) as r:
                if expect_range:
                    # resumed request: the response must cover [offset, ...]
                    cr = r.headers.get("Content-Range", "")
                    m = re.match(r"^bytes (\d+)-(\d+)/(\d+|\*)$", cr.strip())
                    if not m or int(m.group(1)) != offset:
                        raise TransientError(
                            f"resume rejected: Content-Range {cr!r} does not start at {offset}")
                    limit_left = int(m.group(2)) - offset + 1
                else:
                    # fresh full-body request: content-length bounds the write
                    cl = r.headers.get("Content-Length", "")
                    limit_left = int(cl) if cl.isdigit() else None
                for chunk in r.iter_content(CHUNK_SIZE):
                    if chunk:
                        yield chunk

        for chunk in body():
            if limit_left is not None:
                if len(chunk) > limit_left:
                    raise TransientError("server sent more bytes than announced")
                limit_left -= len(chunk)
            atomicio.write_at(root, name, pos, chunk)
            pos += len(chunk)
        return pos - offset

    # -------------------------------------------------------------- progress ui
    def _render_loop(self, scheduler: SegmentScheduler, stop: threading.Event):
        """v5.1-style console renderer + admission status line (real time)."""
        sleeper = RealSleeper()
        started = time.monotonic()
        samples: list[tuple[float, int]] = []
        while not stop.is_set():
            snapshot = scheduler.snapshot()
            done = sum(s.done for s in snapshot)
            total = sum(s.total for s in snapshot) or 1
            counts = scheduler.status_counts()
            now = time.monotonic()
            samples.append((now, done))
            samples = [(t, b) for t, b in samples if t >= now - 8]
            speed = 0.0
            if len(samples) > 1:
                dt = samples[-1][0] - samples[0][0]
                speed = (samples[-1][1] - samples[0][1]) / max(dt, 0.001)
            avg = done / max(now - started, 0.001)
            eta = (total - done) / speed if speed > 0 else None
            eta_s = "--:--:--" if eta is None else (
                f"{int(eta)//3600:02d}:{(int(eta)%3600)//60:02d}:{int(eta)%60:02d}")
            pct = done / total * 100
            fill = int(32 * pct / 100)
            bar = "█" * fill + "░" * (32 - fill)
            sys.stdout.write(
                "\033[2J\033[H"
                f"{self._target.name}\n"
                f"[{bar}] {pct:6.2f}%\n"
                f"Downloaded  {done/1024**3:8.2f} / {total/1024**3:.2f} GiB\n"
                f"Speed       {speed/1024**2:8.1f} MiB/s    Average {avg/1024**2:8.1f} MiB/s\n"
                f"ETA         {eta_s}   Workers {self.cfg.workers}"
                f"   Active {counts['DOWN']}\n"
                f"Limit       {self.admission.governor.limit:3d}"
                f"   Breaker {self.admission.breaker.state.value}\n"
                f"Segments    DONE {counts['DONE']:4d}/{len(snapshot):<4d} "
                f"RETRY {counts['WAIT']:3d}  FAIL {counts['FAIL']:3d}\n"
            )
            sys.stdout.flush()
            sleeper.sleep(0.5)

    # -------------------------------------------------------------- utilities
    def _set_fatal(self, exc: BaseException) -> None:
        with self._fatal_lock:
            if self._fatal_reason is None:
                self._fatal_reason = exc
        if self.cfg.verbose:
            print(f"[gofast52] fatal: {exc}", file=sys.stderr)

    def _log(self, msg: str) -> None:
        if self.cfg.verbose:
            print(f"[gofast52] {msg}", file=sys.stderr)
