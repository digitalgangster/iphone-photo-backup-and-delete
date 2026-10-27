@echo off
setlocal
cd /d "%~dp0"
title iCloud Photo Cleaner

rem ---- find Python 3.10+ (Windows, not WSL) ----
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.10 or newer was not found. Run "Run Photo Extractor.bat" first - it sets Python up.
  pause
  exit /b 1
)

rem ---- separate private environment so the extractor's setup is never disturbed ----
if not exist ".venv-icloud\Scripts\python.exe" (
  echo Setting up the iCloud tool - first run only...
  %PY% -m venv .venv-icloud || goto :fail
)
".venv-icloud\Scripts\python.exe" -c "import pyicloud" >nul 2>nul
if errorlevel 1 (
  echo Installing pyicloud 2026.10.0 - first run only...
  ".venv-icloud\Scripts\python.exe" -m pip install --disable-pip-version-check -q "pyicloud==2026.10.0" || goto :fail
)

".venv-icloud\Scripts\python.exe" icloud_cleaner.py
exit /b 0

:fail
echo.
echo Setup failed. Copy the messages above into a GitHub issue for this project.
pause
exit /b 1
