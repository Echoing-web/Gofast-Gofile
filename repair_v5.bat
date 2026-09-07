@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\GoFast-GoFile-v5.1.exe" (
  echo 请先运行 build_exe.bat 构建程序。
  pause
  exit /b 1
)
set /p V5DIR=请输入 v5 状态文件夹的完整路径:
dist\GoFast-GoFile-v5.1.exe repair-v5 --v5-dir "%V5DIR%"
pause
