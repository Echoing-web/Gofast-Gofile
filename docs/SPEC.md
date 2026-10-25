# GoFast-GoFile v5.2 规格说明书（SDD）

状态: 已评审 (Review Round 3) + v5.3 增补（§8）
日期: 2026-10-25
范围: 针对 v5.1 "并行下载请求过多触发服务器熔断" 问题的架构演进；
      v5.3 增补：下载提速、包体瘦身、内存泄漏防线与发布门槛

---

## 1. 问题陈述（源自 v5.1 实测缺陷）

对 v5.1 `gofast.py` 与 Recovery v5.1 补丁的逐行审查确认以下缺陷，v5.2 必须逐项解决：

| # | 缺陷 | v5.1 位置 | 后果 | v5.2 对策 |
|---|------|-----------|------|-----------|
| D1 | `auto_select_workers` 预先以 4→64 档位做突发探测，最多 188 个并发探测请求 | gofast.py:197-223 | 探测本身即触发服务端限流 | 取消突发探测，改为下载过程中 AIMD 爬坡 |
| D2 | 探测全失败时 `max(results)` 按元组比较选中 64 并发（0 速度也选最大档） | gofast.py:221 | 限流后反而以最高并发启动 | 探测逻辑移除；初始并发固定为小值 |
| D3 | 无全局准入控制：64 线程各自独立重试 8 次，退避上限仅 8s 且无 jitter | gofast.py:359-401 | 重试风暴，服务端熔断持续不恢复 | 全局 AdmissionController（熔断器+令牌桶+动态并发）统一放行 |
| D4 | 不解析 `Retry-After` / 限流响应头 | 全文缺失 | 无视服务器指令，加剧惩罚 | 解析 `Retry-After`（秒/HTTP-date）；`X-RateLimit-Remaining: 0` 视为限流 |
| D5 | 64 线程共享一个 `requests.Session`，连接池默认 maxsize=10 | gofast.py:38-48 | 连接churn、额外握手、偶发竞态 | 单 Session + 按并发显式配置 HTTPAdapter 池，`max_retries=0` |
| D6 | 分片任务一次性全量提交；单分片耗尽重试后异常上抛，其余任务继续打满服务器 | gofast.py:324-336 | 故障期持续高压 | 动态调度器按需领取分片 + 全局熔断闸门 + 可取消 |
| D7 | 退避无 jitter，多线程同步重试形成共振波 | gofast.py:400 | 周期性洪峰 | full jitter（AWS 推荐方式） |
| D8 | 每完成一个分片就全量序列化 manifest（O(n²) 总开销）| gofast.py:329-332 | 锁争用、卡顿 | 节流保存（间隔≥2s）+ 完成时强刷 |
| D9 | Recovery 补丁 NUL 检测写成 `b"\\x00"`（4 字符字面量，非 NUL 字节）| scan_partial.py:27 | 全零未下载分片被标记 DONE，产物含数据洞 | 重写恢复扫描（采样+全扫校验）并纳入测试 |
| D10 | 直链 403/410（过期）无刷新机制，重试耗尽即失败 | gofast.py:372-399 | 长任务中途中断 | 引擎检测直链失效→经 API 重新解析→原子换 URL |
| D11 | 单连接回退模式完全无重试 | gofast.py:436-451 | 瞬时错误即中断 | 复用同一准入/重试管线 |
| D12 | Ctrl+C 后 executor 等待所有排队任务，退出迟滞 | gofast.py:324 | 用户体验 | cancel_futures + 快速排空 |
| D13 | 无 SSRF/URL 校验（对下载器属纵深防御）| 缺失 | 重定向可被引向内网 | URLGuard：仅 http/https，拒绝环回/私有/保留地址（含逐跳重定向校验） |
| D14 | 每次运行都新建 guest 账号（无复用）| gofast.py:151 | 频繁建号是已知的 IP 封禁诱因（cyberdrop-dl #1866、gallery-dl 做法）| guest token 持久化复用，失效才重建 |
| D15 | API JSON 错误未分类（error-rateLimit / unavailable 等仅抛通用错误）| gofast.py:80-101 | 限流响应被当成普通失败重试 | api 层识别 error-rateLimit→Throttled、401/unavailable→FatalError（含 salt 失效提示） |

## 2. 设计目标

G1 **不再触发熔断**：任何时刻对外请求速率与并发都受客户端主动约束；服务端返回限流信号时，客户端在冷却期内静默。
G2 **熔断后自愈**：冷却期满以单请求试探（half-open），成功才逐步恢复并发。
G3 **不损失吞吐**：健康状态下并发可爬升至用户上限；约束只在出现限流证据时收紧。
G4 **数据完整性**：任何中断/崩溃/恢复路径下，已完成字节不丢失、不产生虚假完成（D9 修复）。
G5 **向后兼容**：v5.1 manifest、v2 迁移、CLI 子命令与参数全部兼容；v5.2 清单可被 v5.1 读取（新增字段仅附加）。
G6 **可测性**：时间、睡眠、网络、DNS 全部可注入，核心逻辑可在无网络环境确定性验证（TDD/BDD 基础）。
G7 **无人值守安全**：熔断卡死超过上限时间自动放弃并保存现场，而不是无限重试过夜。

## 3. 架构总览

分层 + 端口-适配器（六边形轻量版）。领域层（调度/控制）不依赖 requests，仅依赖注入的协议接口。

```
┌─────────────────────────── cli.py (表示层/入口) ───────────────────────────┐
│ download / recover / repair-v5 / migrate-v2                               │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │
┌─────────────────────────── engine.py (编排层) ────────────────────────────┐
│ DownloadEngine: 解析→建账→线程池动态调度→进度→收尾                          │
│  依赖: AdmissionController, SegmentScheduler, Transport, ManifestStore,    │
│        LinkRefresher, ProgressUI                                          │
└──────┬──────────────────┬───────────────────┬─────────────────────────────┘
       │                  │                   │
┌──────▼───────┐  ┌───────▼────────┐  ┌───────▼─────────┐
│ admission.py │  │ scheduler.py   │  │ transport.py    │  ← 端口(协议) + 适配器
│  ratelimit   │  │ 分片领取/回队   │  │ RequestsTransport│    (FakeTransport 在测试中)
│  breaker     │  └────────────────┘  └─────────────────┘
│  governor    │
└──────────────┘
       │
┌──────▼───────────────────────────────────────────────────────────────────┐
│ 领域支撑: timeutil.py (Clock/Sleeper) · errors.py (错误分类) ·            │
│ backoff.py · ssrf.py · manifest.py · api.py · recover.py · migrate.py    │
└──────────────────────────────────────────────────────────────────────────┘
```

**关键不变量**：任何线程发出任何 HTTP 请求前必须先通过 `AdmissionController.acquire()`；请求结果必须通过 `report_*` 回报。这是消除 D3/D6 的结构性保证。

## 4. 模块契约

### 4.1 timeutil — 时间与睡眠 seam（G6）

```python
class Clock(Protocol):
    def now(self) -> float: ...          # 单调秒
class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...
class RealClock: ...                     # time.monotonic / time.sleep
class FakeClock(Clock, Sleeper): ...     # sleep(t) 即令 now += t；测试用
```
契约: `FakeClock.sleep` 不真正阻塞，自动推进时间；所有定时逻辑（退避/熔断/桶）只允许经由注入的 Clock/Sleeper 获取时间，违者测试无法确定性。

### 4.2 errors — 错误分类（单义，穷尽）

```python
class DownloadError(Exception): kind: str
class Throttled(DownloadError):    # 429 / 503；retry_after: float|None（已解析秒数）
class TransientError(DownloadError)# 其他 5xx、超时、连接错误、206 Content-Range 不符
class LinkExpired(DownloadError)   # CDN 403/410（直链过期，可刷新）
class FatalError(DownloadError)    # 400/401/403(API)/404、结构损坏等，不可重试
```
契约: Transport 负责把 HTTP/网络事实翻译成且仅翻译成以上四类；上层依类别路由处理，禁止 `except Exception` 吞分类。

### 4.3 backoff — 退避策略（D4/D7）

```python
class BackoffPolicy:
    def __init__(self, base=1.0, factor=2.0, cap=60.0, jitter="full", rng=None): ...
    def delay(self, attempt: int, retry_after: float|None = None) -> float
```
契约:
- 基础序列 `min(cap, base * factor**attempt)`；
- jitter=full 时返回 `uniform(0, 基础序列)`（AWS 架构博客推荐，tenacity `wait_exponential_jitter` 同款）；
- `retry_after` 非空时返回 `min(max(基础序列, retry_after + uniform(0,1)), 1800)`——尊重服务器指令并附加小抖动，天花板 30min（urllib3 retry_after_max=21600 的收紧）；
- 返回值恒 ≥ 0；jitter 前的上界随 attempt 单调不减。

### 4.4 ratelimit — 令牌桶（D3）

```python
class TokenBucket:
    def __init__(self, rate: float, burst: int, clock: Clock): ...
    def acquire(self, tokens: int = 1, sleeper: Sleeper) -> None  # 睡至有足够令牌
```
契约: 桶容量=burst，稳态补充 rate/s；`acquire(1)` 在串行调用下等待时间 ≈ 1/rate（FakeClock 验证 ±ε）；线程安全。
默认 rate=4/s、burst=并发上限（依据调研: 客户端限流取服务端限额 ~80%、burst ≥ 并发数保证启动波放行；golang.org/x/time/rate 惯例）。长连接流式传输期间不消耗令牌，只有发起请求时消耗——这正是平滑"64 分片同时完成→64 个新请求"突发的关键。

### 4.5 breaker — 熔断器（标准三态，G2）

```python
class CircuitState(Enum): CLOSED; OPEN; HALF_OPEN
class CircuitBreaker:
    def __init__(self, throttle_threshold=5, transient_threshold=8,
                 cooldown=60.0, probes=2, max_cooldown=600.0, clock: Clock): ...
    def before_request(self, sleeper: Sleeper, clock=None, cancel=None) -> None
        # OPEN→睡到冷却；HALF_OPEN→仅放行探测名额；cancel 置位→OperationCancelled
    def on_throttled(self, retry_after: float|None) -> None
    def on_transient(self) -> None      # 计入软失败
    def on_success(self) -> None        # HALF_OPEN 探测全成功→CLOSED 并清零计数
    @property state -> CircuitState; @property open_until -> float|None
    total_stuck_cooldown: float         # 自上次 CLOSED 起累计的 open/reopen 冷却秒数
```
契约（默认值依据: resilience4j waitDuration=60s、pybreaker fail_max=5/reset 60s、评审轮 R2 调研）:
- 连续 `throttle_threshold` 个 Throttled → OPEN，冷却 `max(cooldown, retry_after)`；
- 连续 `transient_threshold` 个 TransientError → OPEN（软失败合并计数）；
- OPEN 期间 `before_request` 阻塞至 `open_until`，随后转 HALF_OPEN，**同刻仅放行 `probes`(默认2) 个探测请求**（原子），其余继续等待；
- 全部探测成功→CLOSED；任一探测失败→重新 OPEN（冷却 ×1.5，封顶 `max_cooldown`=10min）；
- `on_success` 在 CLOSED 态清零连续失败计数。

### 4.6 governor — AIMD 自适应并发（D1/D2/G3）

```python
class ConcurrencyGovernor:
    def __init__(self, initial=4, maximum=16, minimum=2,
                 success_streak_to_grow=10, grow_step=1,
                 decrease_factor=0.5, grow_cooldown=10.0, clock: Clock): ...
    def on_success(self) -> int    # 连续成功达阈值且距上次降级>grow_cooldown → limit+grow_step（封顶 maximum）
    def on_throttled(self) -> int  # limit = max(minimum, ceil(limit*decrease_factor))；重置连胜；记录降级时刻
    def on_transient(self) -> int  # limit = max(minimum, limit-1)
    @property limit -> int
```
契约: limit ∈ [minimum, maximum] 恒成立；降级立即生效；增长受冷却与连胜双重门槛（防振荡）；同类事件幂次不使 limit 越界。
默认值依据（评审轮 R2/R3 调研汇总）: GitHub 侧建议 initial≈max×50%、减少因子 0.5；linux.do/同业侧（pixeldrain 5 并发/IP 先例、无官方数字）建议安全并发 ≤8——取折中: initial=4、minimum=2 慢启动，maximum=用户 workers（默认 16），爬坡需每 10 个连续成功 +1（到达 16 需约 120 个健康分片，实际运行会自然收敛到服务器容忍度附近，兼顾 G1/G3）。

### 4.7 admission — 全局准入控制器（核心，不变量的执行者）

```python
class AdmissionController:
    def __init__(self, bucket, breaker, governor, sleeper): ...
    def acquire(self) -> Permit    # 顺序: breaker.before_request → bucket.acquire → DynamicBoundedSemaphore(governor.limit)
    def report_throttled(self, retry_after), report_transient(), report_success()
class DynamicBoundedSemaphore:
    def set_limit(self, n); def acquire(self); release in Permit
```
契约:
- `Permit` 释放即归还并发额度；limit 下调不抢占已发放许可，只是新 acquire 等待；
- breaker OPEN 时 acquire 阻塞（长任务过夜安全：等待以 250ms 粒度轮询取消事件）；
- 所有 report 必须幂等安全（同一请求重复 report 取首个）。

### 4.8 scheduler — 分片动态调度（D6）

```python
@dataclass Segment: index; start; end; done=0; status="WAIT"; retries=0
class SegmentScheduler:
    def __init__(self, segments: list[Segment], max_retries=8): ...
    def claim(self) -> Segment|None       # 线程安全；领取下一个未完成分片（index 升序，符合 CDN 局部性）
    def complete(self, seg)               # 校验 done==total 后置 DONE
    def requeue(self, seg, count_retry=True)  # 保留 done；Throttled 走 count_retry=False（限流是全局拥塞信号，不消耗分片预算）
    def exhausted(self) -> bool           # 所有分片 DONE，或所有未完成分片 retries 耗尽
    def snapshot(self) -> list[Segment]   # 深拷贝，供 manifest/进度
```
契约: claim 在并发下不重领（锁保证同一分片同一时刻只有一个 owner）；`requeue` 后的分片可被再次 claim，但 `retries >= max_retries` 的分片不再发放。

### 4.9 manifest — 原子清单（D8/G4/G5）

> 实现注记：所有磁盘写入经由 `atomicio` 模块（SPEC 4.9a）：realpath 规范化 + 禁止
> 父目录引用 + commonpath 限定在状态目录内，然后 tmp 写入 → fsync → 原子替换 →
> 目录 fsync（尽力而为）。此形态同时满足生成期安全约束（Mimosa 路径穿越拦截）与
> 崩溃原子性，是两者的交集实现。

```python
class ManifestStore:
    def load(self) -> Manifest|None       # 兼容 v5.1 (version:5) 与 v5.2 (version:52)
    def save(self, m: Manifest, force=False)  # 节流≥2s；force 用于收尾/异常退出；内部锁串行化
```
契约:
- 写入: tmp 文件 → flush → fsync → os.replace（同目录原子）→ 目录 fsync（best-effort）；
- v5.1 清单加载后 `done` 语义不变；v5.2 附加字段（aimd 等）对 v5.1 向后可读（未知字段忽略）；
- 读取到损坏 JSON → 返回 None 并保留原文件等待 recover（绝不覆盖现场）。

### 4.10 transport — 传输端口与适配器（D5/D13/G6）

```python
class Transport(Protocol):
    def stream(self, url, headers, timeout) -> ContextManager[Response]  # Response: status_code, headers.get, iter_content(n), close
class RequestsTransport:
    def __init__(self, pool_size, verify=True): ...
```
契约:
- 单 Session + `HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)`；
- 禁用 urllib3 隐式重试（重试权在 admission 层）；
- 手动逐跳跟随重定向（≤5 跳），**每跳过 URLGuard 校验**，跨跳保留原 headers（含 Range）；
- 状态码翻译: 429/503→Throttled（解析 Retry-After: 秒数或 HTTP-date）；403/410→LinkExpired；其余 4xx→FatalError；5xx/超时/连接错误→TransientError；Range 请求得非 206 或 Content-Range 起止不符→TransientError。

### 4.11 ssrf — URL 防护（D13，Mimosa 约束）

```python
class URLGuard:
    def __init__(self, resolver: Callable[[str], list[str]] = socket.getaddrinfo 封装): ...
    def validate(self, url: str) -> None   # 违规则抛 FatalError
```
契约:
- scheme 仅 http/https；
- host 为 IP 字面量或解析结果中**任一**地址命中拒绝集即拒绝：环回(127/8,::1)、私有(10/8,172.16/12,192.168/16,fc00::/7)、链路本地(169.254/16,fe80::/10)、保留(0/8,100.64/10,192.0.0/24,192.0.2/24,198.18/15,198.51.100/24,203.0.113/24,2001:db8::/32,224/4,240/4,::/128,::ffff:0:0/96 映射私有)、广播(255.255.255.255)；
- 名字 "localhost" 或解析失败 → 拒绝；
- resolver 注入（测试用 Fake resolver，不联网）。

### 4.12 api — GoFile API 客户端

契约: 沿用 v5.1 的 website_token 算法（UA::en-US::token::slot::salt 的 sha256）、account 创建、content 查询、flatten 展开与多文件交互选择；所有请求经注入 Transport 与 AdmissionController；错误按 4.2 分类；重试由 admission 统一承担（api 层不再自带 8 次循环）。

社区调研驱动的增补（评审轮 R3）:
- **guest token 持久化复用**: 默认缓存到 `~/.gofast52/guest_token.json`；启动时优先复用缓存 token（gallery-dl 同款做法），仅当 API 明确拒绝该 token 时才新建账号并更新缓存——把"每次运行新建账号"这一已知 IP 封禁诱因（cyberdrop-dl #1866）消除；
- `--token` 传入用户自己的 premium token 时跳过建号（gofile-dl 记载 premium 可绕过 guest 限流）；
- API JSON `status != ok` 时: `error-rateLimit` → Throttled；`error-unavailable`/401 → FatalError 并提示"X-Website-Token salt 可能已轮换，可用 GOFILE_WT_SALT 环境变量覆盖"；
- X-Website-Token 的 salt 默认值保留 v5.1 的 `12af056dacea0b`，支持 `GOFILE_WT_SALT` 覆盖（gallery-dl #9183: salt 不定期轮换，历史值 4fd6sg89d7s6 / gf2026x / 5d4f7g8sd45fsd）。

### 4.13 engine — 下载编排

```python
class DownloadEngine:
    def __init__(self, cfg: EngineConfig, transport, clock, sleeper, ui=True): ...
    def run(self) -> Path
```
行为规格:
1. 解析账号/内容/直链，URLGuard 校验直链；
2. 单请求探测 Range 支持（不再突发测速）；不支持→单连接模式（带准入与退避重试）；
3. 载入或新建清单；partial 只扩不缩、过大即拒（继承 v5.1 语义）；
4. 工作线程 N=max_workers（线程数固定，实际并发由 governor 动态收缩），循环:
   `seg = scheduler.claim()` → None 则退出；
   `permit = admission.acquire()` → `transport.stream(url, Range)` → 校验 206/Content-Range → 就位写入 → 分块落盘、锁内推进 done → 成功回报；
   异常按类回报并 requeue（保 done）/ 换链重试（LinkExpired→LinkRefresher，30s 内至多 1 次）；
5. 引擎放弃条件: scheduler.exhausted 且有未完成分片；或熔断器累计冷却 `total_stuck_cooldown` > `max_open_min`×60（默认 15min，G7）→ 保存现场后抛出可读错误。判据用累计冷却而非状态采样：多线程下 OPEN 窗口在真实时间中可能被压缩到极短而系统实际卡死（评审轮 A-R4）；
6. 全部完成: 强制 fsync partial → 原子改名 → 清理状态目录（失败不影响产物）；
7. KeyboardInterrupt: 取消排队任务、保存清单、快速退出（≤2s）;
8. 进度 UI 继承 v5.1 版式，追加 `Limit {governor.limit} | Breaker {state}` 行。

### 4.14 recover — 恢复扫描（D9 修复）

契约:
- 输入: 状态目录（内含恰好一个 `.partial`）；已有清单仅作为分片尺寸/目标名提示；
  **总量以 partial 文件本身为准**（清单可能正是过期的，这才是运行 recover 的原因；
  不一致时打印警告）；
- 按 segment_mib（清单提示或 `--segment-mib`，默认 64）切块；每块先采样（每 256KiB
  取 16KiB）判非全零→DONE；采样全零→整块精扫确认全零才判 WAIT（防采样误判产生假洞）；
  `--strict` 跳过采样直接全量精扫；
- **正确检测 NUL 字节（`b"\x00"`）**——修复 v5.1 补丁的字面量 bug；
- 输出 v5.2 清单，`created_by: "gofast v5.2 recover"`；绝不修改 partial 本身；
- 合法全零分片保守判 WAIT（重下，宁可贵不可错）。

### 4.15 migrate — v2 迁移

契约: 行为与 v5.1 `migrate_v2` 一致（进程检测拒迁、v2 目录只读、part 逐块拷贝校验 mtime/size、partial 只扩不缩），产物清单 version 升为 52。复用 manifest 模块写入。

### 4.16 cli — 命令行

契约: 子命令 `download / recover / repair-v5 / migrate-v2` 与 v5.1 参数全兼容；`download` 新增: `--req-rate`（令牌桶速率，默认 4/s）、`--max-open-min`（熔断放弃阈值，默认 15）、`--no-adaptive`（禁用 AIMD，固定并发=workers）、`--token`（用户自有账号 token）、`--verbose`（关键准入/链接事件输出到 stderr）。`--auto-workers` 保留但语义变为 AIMD 自适应（等效默认行为），打印提示。

## 5. 关键时序规格（BDD 场景来源）

S1 正常下载: acquire→206→写盘→complete→…→改名。
S2 限流: 429(带上 Retry-After: 30) ×3 → breaker OPEN≥30s → governor 减半 → 冷却后半开单探 → 200 成功 → CLOSED → 并发爬坡恢复。
S3 直链过期: CDN 403 → LinkExpired → 刷新（≤1次/30s）→ 新 URL 续传同分片。
S4 中断恢复: Ctrl-C → 清单落盘 → 重启 → claim 从未完成分片继续，已 done 字节不重下。
S5 恢复扫描: 3 分片中第 2 块全零 → 扫描后仅第 2 块 WAIT。
S6 安全: 直链主机为 10.0.0.5 / 重定向跳 127.0.0.1 / scheme=ftp → 全部 FatalError，零外发请求。
S7 兼容: v5.1 manifest 被 v5.2 加载续传并完成；产物清单可被 v5.1 读取（忽略未知字段）。

## 6. 非功能规格

- 性能: 16 并发、64MiB 分片、千兆链路下，准入控制开销（桶+闸门）CPU 占用 <1%（无 sleep 轮询忙等，用事件/条件变量）。
- 日志: `--verbose` 时 stderr 输出结构化事件（时间/事件/并发/熔断态），默认安静。
- 兼容: Python 3.10+；依赖仅 `requests`（运行时）/ `pytest pytest-bdd`（开发）。
- 构建: 提供 `build_exe_v52.bat` + spec，不触碰 v5.1 的构建文件。

## 7. 验收准则（映射目标）

A1 (G1): 模拟服务器脚本化 429 时，FakeTransport 记录的请求间隔与并发曲线满足: 冷却期内零新请求；并发下降速度符合 0.5 因子。
A2 (G2): half-open 期间并发=1 且失败即重新 OPEN。
A3 (G3): 连续成功路径下 limit 从 4 爬升至 max（FakeClock 无需真实等待）。
A4 (G4): 每个分片写入偏移与内容逐字节断言；恢复扫描对全零块判 WAIT。
A5 (G5): v5.1 清单/参数/子命令回归测试全绿。
A6 (G6): 全部测试无网络、无真实 sleep（CI 可跑）。
A7 (G7): 熔断总时长超限 → 引擎在有限时间内终止并保留现场。


---

## 8. v5.3 增补规格（性能/瘦身/发布门槛）

### 8.1 背景与目标

- G8 提速：在不触发服务端限流的前提下提升实际吞吐（尾段并行度、IO 系统调用削减、
  连接复用深化）；"绕开限制阀门"的口径 = 正当规避触发（请求模式优化、服务器信号
  尊重、premium 通道），不做身份轮换/协议规避。
- G9 瘦身：代码去冗余 + PyInstaller 排除清单，产物体积对比留档。
- G10 资源防线：新增内存泄漏/线程泄漏测试组为发布必过门槛。
- G11 发布流程：单元/回归/冒烟/内存四门全绿 → develop 分支。

### 8.2 F1 尾段动态分裂（tail splitting）

动机：固定分片下，下载尾段只剩少量分片时并行度骤降（90GiB/64MiB 的最后阶段可能
只有 1-2 片在跑）。aria2 以 --min-split-size + 动态再切分解决。

契约:
- `SegmentScheduler.maybe_split(min_split_bytes, desired_parallelism) -> bool`：
  当（DOWN 数 + 可领取 WAIT 数）< desired_parallelism 且存在剩余量 >= 2×min_split
  的未领取 WAIT 分片时，把它对半分裂为两个 WAIT 分片；done 前缀归左半（钳制），
  右半 done=0、retries=0；内部列表按 start 排序，claim 顺序 = start 顺序；
  snapshot() 时按 start 序重新编号 index（对 v5.1 加载无连续性要求）。
- 不分裂 DOWN（在飞）分片 v5.3（owner 响应覆盖旧区间，强拆会触发超发钳制）；记为
  已知限制。
- 引擎 worker：claim 得到 None 且未耗尽时，先 maybe_split 再 claim 一次。
- 验收 A8: 大文件少 worker 场景，尾段期间活跃并行度高于固定分片基线；分裂后
  文件内容逐字节正确。

### 8.3 F2 分段缓冲写（syscall 削减）

契约:
- `atomicio.SegmentWriter`（上下文管理器）：构造时按既定校验打开 root/filename
  一次（os.open O_RDWR），`write(offset, data)` 内部 lseek+write 循环，退出时关闭；
  不做隐式 fsync（收尾统一 fsync，见 4.13 步骤 6）。
- `_fetch_segment` 每段尝试持有一个 SegmentWriter，替代逐 1MiB 块的 open/close。
- iter_content 块 1MiB → 4MiB（CHUNK_SIZE 常量，文档注明）。
- 验收 A9: SegmentWriter 与 write_at 校验语义一致（单测）；每段尝试仅一次 open。

### 8.4 F3 Retry-After 感知的熔断冷却（已实施，本轮固化契约）

契约: 带显式 Retry-After 的限流，冷却 = clamp(ra, 2s, max_cooldown)，重开不再
无脑 ×1.5 升级（服务器明示"等 2 秒"不得升级成数分钟）；无 ra 信号才按 ×1.5 递增。
验收 A10: 10 次 ra=2 的风暴在累计冷却远小于 15min 时恢复并完成下载（压力组断言）。

### 8.5 F4 代码瘦身（G9）

- 删除 engine._guarded 与 run_guarded 的重复实现（统一走 admission.run_guarded）。
- transport 的 email.utils 改为按需导入（仅 HTTP-date 解析路径）。
- 全模块过一遍未用导入/死分支。
- PyInstaller：排除清单（pytest 系、tkinter、unittest、pydoc 等，以实测不破坏
  requests 为准）+ 体积对比表留档于 DESIGN.md。

### 8.6 G10 资源防线（tests/test_memory.py）

- 线程泄漏：6 次完整引擎运行后 threading.active_count() 回到基线。
- 取消唤醒：阻塞在并发闸门的等待者全部随 cancel 退出。
- 堆增长：8 次运行 tracemalloc 增长 < 4MiB；gc.garbage 不增长。
- 资源卫生：分类异常路径的响应对象全部 close。

### 8.7 G11 发布门槛（全绿才允许推送 develop）

门1 批量单元：全量 pytest 通过。
门2 多组回归：核心组（breaker/admission/scheduler/governor/ratelimit）+ 引擎组 +
IO 兼容组（manifest/migrate/recover/cli）+ BDD 组 + 压力组（test_stress），分组各跑一遍。
门3 冒烟：test_smoke（子进程级 --help/recover/repair + 真实装配引擎的脚本化下载）。
门4 内存：test_memory 全绿。
