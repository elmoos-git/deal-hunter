#!/bin/bash
# Double-click to start Deal Hunter (Linux). First run installs everything (needs internet, takes a minute).
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up for the first time..."
  python3 -m venv .venv || { echo "Python 3.11+ is needed: install it from python.org, then try again."; read -r; exit 1; }
  .venv/bin/python -m pip install --quiet --upgrade pip
fi
.venv/bin/python -m pip install --quiet -r requirements.txt
echo "Deal Hunter is starting at http://127.0.0.1:8000  (close this window to stop it)"
(sleep 3; xdg-open http://127.0.0.1:8000) &
.venv/bin/python run.py
