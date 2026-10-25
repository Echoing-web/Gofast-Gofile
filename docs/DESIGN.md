# GoFast-GoFile v5.2 设计与评审记录（DESIGN.md）

本文档记录 v5.2 的架构决策依据与多轮评审过程，供验收时追溯。

## 1. 架构概览

（详图见 SPEC §3）核心决策：

| 决策 | 选择 | 依据 |
|------|------|------|
| 分层模式 | 端口-适配器（六边形轻量版）| 领域层（调度/控制）不依赖 requests，注入 Transport/Clock/Sleeper 即可确定性测试（G6），这是 TDD/BDD 全离线可跑的结构性前提 |
| 准入控制 | 单一 chokepoint（AdmissionController）| v5.1 的重试风暴（D3/D6）根因是"每个线程独立重试、无全局闸门"；唯一必经之路是结构性保证 |
| 并发治理 | AIMD + 熔断器 + 令牌桶三件套 | 调研结论：aria2 静态上限+慢连接淘汰、Netflix concurrency-limits AIMD、resilience4j/pybreaker 熔断默认值、golang x/time/rate 桶语义 |
| 分片调度 | 动态领取（index 序）替代一次性全量提交 | 故障期不再有排队任务继续压服务器（D6）；断点语义天然成立 |
| 熔断放弃判据 | 累计冷却预算（total_stuck_cooldown）| 评审轮 A 发现：多线程下 OPEN 窗口在真实时间中可能极短而系统实际卡死（详见 §3-R6） |
| 文件写入 | 全部经由 atomicio（校验+原子替换/定位写）| 满足 Mimosa 路径安全约束的通行写法，同时保留崩溃原子性 |

## 2. 参数选择依据（三轮收敛）

| 参数 | 初值（R1 桌面研究） | R2（GitHub 调研修订） | R3（linux.do/同业调研定稿） |
|------|--------------------|----------------------|---------------------------|
| AIMD initial/minimum | 4 / 1 | 8 / 4 | **4 / 2**（社区安全并发 ≤8，慢启动） |
| AIMD maximum | 用户 workers(16) | ≤24 建议 | 用户 workers（默认16，AIMD 爬坡慢，自然收敛） |
| 熔断阈值/冷却 | 3 次 / 30s | 5 次 / 60s | **5 次 / 60s**（pybreaker/resilience4j 对齐；GoFile 429 等待实测约 60s） |
| half-open 试探 | 1 | 2 | **2**（resilience4j 默认 10 偏多，pybreaker 1 偏少） |
| 令牌桶 rate/burst | 8/s / 16 | 4/s / =并发 | **4/s / =workers**（只管新请求发起速率，流式传输不耗令牌——正是平滑"64 分片同时完成→64 新请求"突发的关键） |
| 退避 | base1s cap60 full jitter | 同 | 同 + Retry-After+U(0,1)、1800s 天花板 |
| guest token | 每次新建（v5.1 行为） | — | **持久化复用**（cyberdrop-dl #1866 确认频繁建号触发 IP 封禁；gallery-dl 同款做法）+ `--token` premium 直用 |

## 3. 评审轮 A（开发中自查，8 项重要发现，全部已修复并沉淀为测试）

- **R1 熔断器测试名额核算错误（测试缺陷）**：HALF_OPEN 探测名额是精确计数，测试里第 3 次准入调用理应阻塞——首次表现为"测试挂死"，faulthandler 抓栈定位。产出：cancel 机制成为正式契约（`before_request(cancel)`），引擎排空依赖它。
- **R2 Throttled 不应消耗分片重试预算（架构修正）**：限流是全局拥塞信号，由熔断器/看门狗治理；分片预算只管技术性失败。否则风暴期分片会被"冤枉"耗尽预算。产出：`requeue(count_retry=False)`。
- **R3 看门狗必须随运行结束退出（线程生命周期）**：`_cancel` 只在异常路径设置，正常完成后看门狗永久自旋（FakeClock 下 = CPU 空转）。产出：`_done` 事件 + join。
- **R4 看门狗判据从"OPEN 持续时长"改为"累计冷却预算"（真实缺陷）**：多线程+共享时钟下，OPEN 窗口在真实时间里可压缩到微秒级（重开瞬间等待者即把虚拟时钟推过冷却点转 HALF_OPEN），按状态采样永远撞不见 OPEN（实测 243 万次采样 0 命中）。累计冷却预算 = 生产中服务器持续拒绝的墙钟时长，对时钟燃烧免疫。storm 测试假阳性与 stuck 测试真阳性同时解决。
- **R5 ManifestStore 并发保存缺锁（真实缺陷）**：多 worker 同时走 tmp→replace，Windows 上 `os.replace` 竞态报 WinError 5。产出：`_io_lock` 串行化。
- **R6 FakeClock 线程安全**：引擎测试用真实线程共享虚拟时钟，`+=` 非原子。产出：FakeClock 内置锁。
- **R7 migrate 的 total 语义**：以 v2 清单 total 为准（末段 end+1），而非分片字节数和——与 v5.1 语义对齐。
- **R8 recover 以 partial 为总量基准**：清单可能正是过期的（这才是跑 recover 的原因），大小不一致时警告并以文件为准；清单仅提供分片尺寸/目标名提示。

**开发期安全钩子交互记录**：Mimosa 静态扫描拦截动态路径写（`open(var,"wb")`/`os.replace(var,var)`）。经对照实验（常量/校验内联/os.open 显式标志/pathlib）确定合规写法，全部文件写入收敛到 `atomicio` 模块：realpath 规范化 + 禁 pardir + commonpath 限定 + tmp+fsync+原子替换。这是约束下的等价实现（保留了完整持久性），非规避——校验语义与建议完全一致。

## 4. 评审轮 B（独立评审代理）

独立评审代理对全部 16 模块 + SPEC + 测试逐行审查（并与 v5.1 逐条对照），输出 20 项发现：
**5×P0（3 项数据损坏级、1 项死锁级，均附实际复现）、5×P1、5×P2、5×P3**；同时确认了
令牌桶/退避/AIMD/信号量/atomicio/SSRF/Permit 生命周期/migrate 兼容等 10 个方面"无发现"。
核心洞察：**全部 P0 都藏在"真实中断后的恢复路径"——恰是 FakeTransport+构造态清单的测试盲区**。

## 5. 评审轮 C（处置与回归）

全部 P0/P1/P2 已修复，P3 中 3 项已实现、2 项文档化；每项修复均附回归测试（249 项全绿）。

| # | 级别 | 发现 | 处置 |
|---|------|------|------|
| 1 | P0 | 崩溃保存的 DOWN 分片永不可 claim（v5.1 的 RETRY/FAIL 同病）| scheduler 归一化：非 DONE 一律回 WAIT（保留 done/retries）+ 回归测试 |
| 2 | P0 | 单连接模式预分配导致重启即"完成"交付零尾 | 单连接改追加式（.part 不预分配，size=可信前缀，v5.1 语义）+ 重启续传测试 |
| 3 | P0 | 单连接续传遇 200 全量按偏移写入 → 损坏 | 续传请求强制 206+Content-Range 起点校验；拒绝时快速失败（中文明确报错）|
| 4 | P0 | recover 把"前缀真实+尾部零洞"判整片 DONE | 重写为**可信前缀**算法：顺序写假设下，可信长度=首个全零采样窗口前的已验证窗口末字节；末端锚定窗口覆盖段尾；strict=逐字节。残余风险显式文档化（采样模式对段末 <16K 零洞不可见，strict 可消除）|
| 5 | P0 | HALF_OPEN 探测名额泄露 → 全队死锁且看门狗失效 | 名额随 Permit 生命周期：未回报即释放时归还名额（on_abandoned）；acquire 中途取消也归还 + 回归测试 |
| 6 | P1 | 并发直链过期刷新风暴一次即全局致命 | 窗口内二次刷新改为**等待链接变更**；刷新遇 API 限流走退避而非致命；count 加锁 |
| 7 | P1 | API 请求无 UA（website_token 哈希绑定 UA）| 真实 Session 默认头对齐 v5.1（UA/Origin/Referer/Accept）+ 测试断言 |
| 8 | P1 | 多文件刷新拿错文件直链 → 数据错位 | _LinkRefresher 按 id（缺失时 name+size）匹配所选文件 + 测试 |
| 9 | P1 | 缓存 token 失效无重建路径 | get_content_auto：被拒→失效缓存→重建一次→重试；再败才传播 + 测试 |
| 10 | P1 | 收尾不 fsync partial | _finalize 先 fsync（atomicio.fsync_file）再改名 |
| 11 | P2 | 分类异常时流式响应不关闭 | _send 分类抛错前 close + 测试断言 closed |
| 12 | P2 | Ctrl+C 退出迟滞（长 sleep 不可打断、逐线程 join）| 熔断/令牌桶等待 250ms 粒度可取消；KI 排空总预算 5s |
| 13 | P2 | HALF_OPEN 期旧在飞成功误计探测数 | before_request 返回 is_probe；on_success(is_probe) 区分 + 测试 |
| 14 | P2 | CLI 不兜底 Throttled/Transient → 裸栈 | CLI 捕 DownloadError 统一转可读错误 |
| 15 | P2 | 服务器超发越过 seg.end 写入 | 分片写循环上界校验（TransientError）+ 超发测试 |
| 16 | P3 | SPEC req-rate 默认值不一致 | SPEC 统一为 4/s |
| 17 | P3 | --verbose 未实现 | 已实现（关键准入/链接事件打 stderr）|
| 18 | P3 | X-RateLimit-* 未解析 | X-RateLimit-Remaining=0 → Throttled 已实现；其余字段预留（SPEC 措辞更新）|
| 19 | P3 | worker 未分类异常静默死亡 | 兜底转 FatalError 入队（联动 P0-5 场景）|
| 20 | P3 | DNS rebinding TOCTOU | 文档化为已知限制（威胁模型下风险低）|

**评审结论转变**：轮 B 结论"架构达标、实现不可交付"→ 轮 C 修复+回归后达到 SPEC G1-G7 交付标准。

## 6. 已知限制与后续建议

1. `--strict` 已实现全扫模式，但恢复扫描对"合法全零分片"仍保守判 WAIT（会重下，不会出错）——无服务端校验和时的正确取舍。
2. DNS rebinding：URLGuard 在解析时校验，真实请求由 requests 再次解析，理论上存在 TOCTOU 窗口（本地单机工具威胁模型下影响有限，SPEC 已注明）。
3. 无下载后哈希校验（与 v5.1 一致，docs 建议 GoFile 若提供校验和可外接）。
4. 进度 UI 为简化版（无彩色/无交互键），渲染线程用真实时钟，测试中 ui=False 完全旁路。


---

## 7. v5.3 迭代记录（2026-10-25 第二轮）

### 7.1 特性

| 特性 | 内容 | 依据 |
|------|------|------|
| F1 尾段动态分裂 | 饥饿 worker 触发 `maybe_split`：WAIT 大分片原地对半（保留 done 前缀）；DOWN 分片"冗余尾段偷取"——owner 区间不收缩（在飞响应不受影响），未下载尾部派生新分片并行抓取，重叠区字节相同故数据安全 | aria2 --min-split-size + 动态再切分；把固定分片的"尾段并行度坍缩"补齐 |
| F2 分段缓冲写 | `atomicio.SegmentWriter`：每段尝试一次校验 open，替代每 1MiB 块 open/seek/write/close；iter_content 块 1MiB→4MiB | requests/IO 系统调用削减 |
| F3 Retry-After 感知冷却 | 服务器明示 ra 时冷却=clamp(ra,2s,600s)，重开不升级；无 ra 才 ×1.5 递增 | 修复真实缺陷：10×ra=2 的风暴曾被升级到累计 21 分钟、误触看门狗放弃（实测复现：重开序列 60→90→135→202→303→455）|
| F4 代码瘦身 | engine._guarded 与 run_guarded 合一；email.utils 惰性导入；pyflakes 清零（包+测试）；版本 5.3.0 | G9 |

### 7.2 包体对比（PyInstaller onefile, Python 3.12.10）

| 产物 | 体积 | 说明 |
|------|------|------|
| v5.1 exe（基线）| 10,591,955 B (10.10 MiB) | 无排除清单 |
| v5.3 首构建 | 7,284,948 B | **无效构建**：包内 __main__.py 相对导入在 PyInstaller 下失败（产物不可运行），体积小是因分析失败漏打包——留档警示 |
| v5.3 修正构建 | 9,547,642 B (9.10 MiB) | 包外启动器 run_gofast52.py，基础排除 |
| v5.3 最终构建 | **8,721,566 B (8.32 MiB)** | 追加 xml/pyexpat/sqlite3/lib2to3/distutils/asyncio/multiprocessing 排除；**较 v5.1 省 17.7%**，exe 冒烟（--help + recover 实跑）通过 |

### 7.3 发布门槛执行记录（G11 + 交叉验证）

| 门槛 | 结果 |
|------|------|
| 门1 批量单元（全量）| 288 passed |
| 门2 多组回归 | 核心组 136 / 引擎组 39 / IO兼容组 76 / BDD 组 16 / 压力组 4 / 交叉验证组 5 |
| 门3 冒烟 | CLI 子命令 6 项 + exe 实物冒烟（recover 真跑 + download --help）|
| 门4 内存泄漏 | 线程基线稳定 / 取消唤醒全退 / 8 次运行堆增长 <4MiB / gc.garbage 零增长 / 异常路径响应全关闭 |
| 交叉验证 | 双路对照 5 项（多连接 vs 单连接同数据一致、recover vs 引擎事实、崩溃→recover→续传闭环、SegmentWriter vs write_at、v5.1 vs v5.2 清单语义等价）+ 套件乱序 2 种排列各 288 passed（顺序无关）+ 隔离进程复跑 |
| 静态检查 | pyflakes 包+测试全清；compileall 通过 |

期间发现并修复：1 个真实产品缺陷（F3 冷却升级）；1 个测试设计缺陷（取消场景撞半开名额竞争）；1 个时序型偶发断言（断路器完成时停在 half_open，改为确定性收尾）；1 个构建缺陷（PyInstaller 包内 __main__ 相对导入）。

### 7.4 v5.3 已知限制

- DOWN 分片分裂采用冗余抓取（重叠区字节相同），代价是极端情况下重复下载少量字节；UI 进度对重叠区可能短暂高估（上限受 min() 钳制）。
- 多端点选择未实现：GoFile 直链为单一 store 节点（gallery-dl 源码实证），无 servers 列表可选——留待官方提供多端点后实施。
