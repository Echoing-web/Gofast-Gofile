@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\GoFast-GoFile-v5.1.exe" (
  echo 请先运行 build_exe.bat 构建程序。
  pause
  exit /b 1
)
echo.
echo 注意:
echo 请先关闭旧版 v2 GoFast 下载。
echo 如果旧版 GoFast 进程仍在运行，本工具会拒绝迁移。
echo 本工具不会结束旧版进程。
echo.
set /p V2DIR=请输入 v2 .gofast_* 文件夹的完整路径:
dist\GoFast-GoFile-v5.1.exe migrate-v2 --v2-dir "%V2DIR%"
pause
