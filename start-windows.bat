@echo off
rem Double-click to start Deal Hunter. First run installs everything (needs internet, takes a minute).
rem Needs Python 3.11 or newer from python.org (tick "Add python.exe to PATH" when installing).
cd /d "%~dp0"
set CHECK=import sys; sys.exit(0 if sys.version_info ^>= (3, 11) else 1)

rem A .venv made with an older Python cannot install the requirements: rebuild it.
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "%CHECK%" 2>nul || rmdir /s /q .venv
)

if not exist .venv\Scripts\python.exe (
  set PY=
  for %%V in (3.13 3.12 3.11) do (
    if not defined PY py -%%V -c "%CHECK%" >nul 2>nul && set PY=py -%%V
  )
  if not defined PY python -c "%CHECK%" >nul 2>nul && set PY=python
  if not defined PY (
    start "" https://www.python.org/downloads/windows/
    echo.
    echo Deal Hunter needs Python 3.11 or newer, and none was found on this PC.
    echo Install the latest Python from the page that just opened, tick "Add python.exe to PATH",
    echo then double-click this file again.
    echo.
    pause
    exit /b 1
  )
  echo Setting up for the first time...
  call %%PY%% -m venv .venv || (echo Creating the virtual environment failed. & pause & exit /b 1)
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
)
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || (echo. & echo Installing requirements failed. Check your internet connection and try again. & pause & exit /b 1)
echo Deal Hunter is starting at http://127.0.0.1:8000  (close this window to stop it)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
.venv\Scripts\python.exe run.py
pause
