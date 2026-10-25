@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\GoFast-GoFile-v5.1.exe" (
  echo Please run build_exe.bat first.
  pause
  exit /b 1
)
set /p V5DIR=Paste the full path of the v5 state folder: 
dist\GoFast-GoFile-v5.1.exe repair-v5 --v5-dir "%V5DIR%"
pause
