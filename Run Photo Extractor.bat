@echo off
setlocal
cd /d "%~dp0"
title iPhone Photo Extractor

rem ---- find Python 3.10+ (Windows, not WSL) ----
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.10 or newer was not found on Windows.
  echo.
  choice /m "Install Python 3.12 now with winget"
  if errorlevel 2 goto :nopython
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo Python installed. Close this window and double-click "Run Photo Extractor.bat" again.
  pause
  exit /b 0
)

rem ---- private environment next to this script (first run only) ----
if not exist ".venv\Scripts\python.exe" (
  echo Setting up a private Python environment - first run only...
  %PY% -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -c "import pymobiledevice3" >nul 2>nul
if errorlevel 1 (
  echo Installing pymobiledevice3 11.28.2 - first run only...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q "pymobiledevice3==11.28.2" || goto :fail
)

rem ---- open the window (falls back to a console view if the window toolkit is missing) ----
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" extractor.py --console
  pause
  exit /b 0
)
start "" ".venv\Scripts\pythonw.exe" extractor.py
exit /b 0

:nopython
echo Install Python from https://www.python.org/downloads/ (check "Add python.exe to PATH"), then run this again.
pause
exit /b 1

:fail
echo.
echo Setup failed. Copy the messages above into a GitHub issue for this project.
pause
exit /b 1
