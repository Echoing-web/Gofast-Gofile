@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\GoFast-GoFile-v5.1.exe" (
  echo Please run build_exe.bat first.
  pause
  exit /b 1
)
echo.
echo IMPORTANT:
echo Close the old v2 GoFast download first.
echo This tool will refuse migration if an old GoFast process is running.
echo It does NOT terminate the old process.
echo.
set /p V2DIR=Paste the full path of the v2 .gofast_* folder: 
dist\GoFast-GoFile-v5.1.exe migrate-v2 --v2-dir "%V2DIR%"
pause
