# GoFast-GoFile v5.1

## 紧急修复：修复“partial 文件过小”迁移问题

如果 v5 启动时出现：

```text
ERROR: v5 partial 文件大小异常: 41005051904 != 92598638079
```

v5.1 不会删除现有 partial，而会把它安全扩展到 manifest 中记录的目标大小，已有字节保持不变。

你的错误属于这种状态恢复问题：

- 目标文件应为约 86.24 GiB = 92,598,638,079 bytes
- 当前 `.partial` 只有 41,005,051,904 bytes
- v5.1 会扩展文件，而不会清除已有数据

## 推荐操作

先不要删除任何 v2 或 v5 文件。

### 方案 A：直接用 v5.1 修复

在 v5.1 文件夹：

```powershell
.\build_exe.bat
```

然后：

```powershell
.\repair_v5.bat
```

输入你的 v5 state 目录，例如：

```text
D:\Download_Chrome\GoFast-GoFile-Windows-v2\downloads\.gofast_v5_WutheringWavesBetaGame.zip
```

成功后再启动：

```powershell
.\dist\GoFast-GoFile-v5.1.exe download "https://gofile.io/d/nqNatuwu" --workers 32 --out "D:\Download_Chrome\GoFast-GoFile-Windows-v2\downloads"
```

### 方案 B：不用 repair_v5.bat

```powershell
.\dist\GoFast-GoFile-v5.1.exe repair-v5 --v5-dir "D:\Download_Chrome\GoFast-GoFile-Windows-v2\downloads\.gofast_v5_WutheringWavesBetaGame.zip"
```

然后再启动下载。

## v2 安全性

v5.1 不会：

- 删除 v2 文件
- 修改 v2 manifest
- 修改 v2 .part
- 结束 v2 进程

迁移仍然要求旧 v2 进程已经停止，以避免读取正在变化的分片。

## v5.1 额外修复

- 已存在的短 `.partial` 自动扩展
- 不删除已有字节
- partial 大于目标时拒绝继续，防止状态错配
- `repair-v5` 命令用于显式恢复
