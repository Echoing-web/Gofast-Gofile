@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m PyInstaller --noconfirm --clean --onefile ^
  --name GoFast-GoFile-v5.3 ^
  --exclude-module pytest ^
  --exclude-module pytest-bdd ^
  --exclude-module _pytest ^
  --exclude-module pyflakes ^
  --exclude-module tkinter ^
  --exclude-module unittest ^
  --exclude-module pydoc ^
  --exclude-module pydoc_data ^
  --exclude-module doctest ^
  --exclude-module setuptools ^
  --exclude-module pip ^
  --exclude-module wheel ^
  --exclude-module test ^
  --exclude-module xml ^
  --exclude-module pyexpat ^
  --exclude-module sqlite3 ^
  --exclude-module lib2to3 ^
  --exclude-module distutils ^
  --exclude-module asyncio ^
  --exclude-module multiprocessing ^
  run_gofast52.py
echo Build finished: dist\GoFast-GoFile-v5.3.exe
