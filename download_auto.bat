@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\GoFast-GoFile-v5.1.exe" (
  echo 请先运行 build_exe.bat 构建程序。
  pause
  exit /b 1
)
set /p URL=请输入 GoFile 链接:
dist\GoFast-GoFile-v5.1.exe download "%URL%" --auto-workers
pause
