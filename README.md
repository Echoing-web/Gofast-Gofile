# GoFast-GoFile v5.3

针对 v5.1 "并行下载请求过多触发服务器熔断" 问题的架构演进版（v5.2 准入控制架构 + v5.3 提速/瘦身/资源防线迭代）。
核心不变量：**所有对外请求必经全局准入控制器**（熔断器 + 令牌桶 + AIMD 自适应并发）；
v5.3 新增**尾段动态分裂**（饥饿 worker 自动再切分大分片，尾段不再掉速）、
**Retry-After 感知冷却**（服务器说等 2 秒就等 2 秒，不再盲目指数升级）、
**分段缓冲写**（每分片一次文件打开 + 4MiB 大块），以及 exe 瘦身（较 v5.1 小 17.7%）。

## 与 v5.1 的关系

- CLI 子命令与参数**完全兼容**：`download / migrate-v2 / repair-v5` 照旧，v5.1 清单（含崩溃态 DOWN/RETRY/FAIL）可直接续传。
- Recovery 补丁合并为内置 `recover` 子命令（可信前缀算法，修复 v5.1 补丁的 NUL 字面量数据洞 bug）。
- guest token 持久化复用（防 IP 封禁），支持 `--token` premium 直用，被拒自动重建。

## 快速开始

```powershell
py -m pip install -r requirements.txt

# 基本下载（默认 AIMD 自适应并发，上限 16）
py -m gofast52 download "https://gofile.io/d/XXXXXX" --out "D:\Download"

# v5.1 参数照常 + v5.2/v5.3 新增
py -m gofast52 download "https://gofile.io/d/XXXXXX" --workers 32 --verbose
#   --req-rate 4        新请求发起速率上限（令牌桶）
#   --max-open-min 15   熔断累计冷却放弃阈值（无人值守安全）
#   --no-adaptive       固定并发，关 AIMD
#   --token XXX         使用自己的（premium）token
#   --verbose           关键准入/链接事件
```

### 恢复 / 迁移

```powershell
py -m gofast52 recover --state-dir "D:\x\.gofast_v5_文件名" [--strict]
py -m gofast52 migrate-v2 --v2-dir "D:\旧目录"
py -m gofast52 repair-v5 --v5-dir "D:\x\.gofast_v5_文件名"
```

## 构建单文件 exe

```powershell
py -m pip install pyinstaller
build_exe_v53.bat
# 产物: dist\GoFast-GoFile-v5.3.exe  (约 8.3 MiB，较 v5.1 小 17.7%)
```

## 测试（发布四门 + 交叉验证）

```powershell
py -m pip install -r requirements-dev.txt
py -m pytest tests -q     # 288 项：单元 + BDD + 压力 + 内存泄漏 + 冒烟 + 双路交叉验证
```

- 单元/TDD：`tests/test_*.py`（核心控制、引擎、IO 兼容）
- BDD：`tests/bdd/features/`（熔断恢复、自适应并发、生命周期、恢复扫描、安全、兼容、v5.3 提速）
- 压力组：`tests/test_stress.py`（24MiB×16workers、节流风暴、重复运行）
- 内存泄漏组：`tests/test_memory.py`（线程基线/取消唤醒/堆增长/gc/响应关闭）
- 冒烟组：`tests/test_smoke.py`（子进程级子命令端到端）
- 交叉验证：`tests/test_crosscheck.py`（多连接 vs 单连接同数据一致、崩溃→recover→续传闭环等 5 项双路对照）

目录：`gofast52/`（包）· `tests/` · `docs/`（SPEC 规格书、DESIGN 评审与迭代记录、REPORT 验收报告）。

## 安全边界

- 仅 http/https；直链与每一跳重定向经 URLGuard 校验，拒绝环回/私有/保留/映射 v6/localhost 类地址。
- 所有本地写入限定在状态目录内（realpath+commonpath+原子替换），收尾先 fsync 再改名。
- token 仅存本地 `~/.gofast52/`。
