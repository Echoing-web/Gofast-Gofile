@echo off
setlocal
cd /d "%~dp0"
set /p URL=Paste GoFile URL: 
dist\GoFast-GoFile-v5.1.exe download "%URL%" --workers 64
pause
