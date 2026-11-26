@echo off
rem Double-click to start Deal Hunter. First run installs everything (needs internet, takes a minute).
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv\Scripts\python.exe (
  echo Setting up for the first time...
  %PY% -m venv .venv || (echo Python 3.11+ is needed: install it from python.org, tick "Add to PATH", then try again. & pause & exit /b 1)
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
)
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
echo Deal Hunter is starting at http://127.0.0.1:8000  (close this window to stop it)
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
.venv\Scripts\python.exe run.py
pause
