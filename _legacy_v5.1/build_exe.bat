@echo off
setlocal
cd /d "%~dp0"
echo [1/2] Installing/updating dependencies...
py -m pip install --upgrade pyinstaller requests
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)
echo [2/2] Building GoFast-GoFile-v5.1.exe ...
pyinstaller --clean --onefile --name GoFast-GoFile-v5.1 gofast.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
echo.
echo Built:
echo %CD%\dist\GoFast-GoFile-v5.1.exe
pause
