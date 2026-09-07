# GoFast-GoFile v5.1

GoFast-GoFile 是一个面向 Windows 的 GoFile 大文件下载工具。它支持多连接 `Range` 下载、断点续传、自动并发测速、v1/v2 到 v5 的状态迁移，以及短 `.partial` 文件修复。

## 功能概览

- **多连接下载**：在服务器支持 HTTP `206 Partial Content` 时，将文件切分为多个分片并发写入同一个临时文件。
- **断点续传**：下载状态保存在目标目录下的 `.gofast_v5_<文件名>` 中，再次执行相同命令即可继续。
- **自动并发测速**：使用少量 `Range` 探测请求，从预设并发数中选择测速结果较好的值；探测不会写入目标文件。
- **单连接回退**：服务器不支持分片下载时，自动使用单连接断点续传。
- **旧版迁移**：读取 v1/v2 的 `manifest.json` 和 `.part` 文件，生成 v5 状态；旧版目录保持不变。
- **状态修复**：将过短的 v5 `.partial` 文件扩展到清单记录的大小，已有字节不会被删除；过大的文件会被拒绝处理。

## 快速开始

### 1. 准备环境

- Windows 10 或更高版本。
- Python 3.10 或更高版本，并确保 `py` 命令可用。
- 网络可以访问 `gofile.io` 及其 API。
- 下载链接应为 `https://gofile.io/d/<内容编号>` 格式。

### 2. 安装依赖并构建程序

在仓库目录打开 PowerShell，执行：

```powershell
py -m pip install -r requirements.txt
.\build_exe.bat
```

构建完成后，可执行文件位于 `dist\GoFast-GoFile-v5.1.exe`。`build_exe.bat` 会安装或更新 `PyInstaller` 和 `requests`。

### 3. 下载文件

直接使用 Python：

```powershell
py .\gofast.py download "https://gofile.io/d/内容编号" --out "D:\Downloads"
```

使用构建后的程序：

```powershell
.\dist\GoFast-GoFile-v5.1.exe download "https://gofile.io/d/内容编号" --workers 32 --out "D:\Downloads"
```

下载中断后，重新执行同一条命令即可继续。默认输出目录是当前目录下的 `downloads`。多连接模式完成时，如果目标位置已有同名文件，旧文件会先改名为 `.old`。

## 常用操作

完整的功能说明、参数表、迁移步骤、修复步骤和排错方法见 [`docs/功能与操作指引.md`](docs/功能与操作指引.md)。

常用 Windows 脚本：

- `download_auto.bat`：输入链接后自动测速并选择并发数。
- `download_64.bat`：输入链接后使用最多 64 个并发连接。
- `handoff_v2_to_v5.bat`：将已停止的 v1/v2 下载状态迁移到 v5。
- `repair_v5.bat`：修复过短的 v5 `.partial` 文件。

## 安全与数据边界

- 迁移前必须先关闭旧版 GoFast 下载进程；工具只会拒绝迁移，不会替用户结束进程。
- v1/v2 的原目录、`manifest.json` 和 `.part` 文件不会被本工具删除或修改。
- 修复操作只会扩展过短的 `.partial`；文件过大或清单不匹配时会停止。
- 程序会向 GoFile 创建临时 API 账号以读取内容信息，不会把账号令牌写入项目文件或日志。
- 请不要把下载链接中的密码、个人路径、运行日志或生成的 `dist` 文件提交到公开仓库。

## 项目结构

```text
gofast.py                 主程序
requirements.txt          Python 依赖
build_exe.bat             构建 Windows 可执行文件
download_auto.bat         自动并发下载入口
download_64.bat           64 并发下载入口
handoff_v2_to_v5.bat      v1/v2 状态迁移入口
repair_v5.bat             v5 状态修复入口
docs/                     功能与操作文档
```

## 许可说明

本仓库当前未附带许可证文件。若要公开发布、二次分发或合并第三方代码，请先补充明确的许可证和来源说明。
