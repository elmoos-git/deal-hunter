#!/bin/bash
# Double-click to start Deal Hunter (Linux). First run installs everything (needs internet, takes a minute).
# Needs Python 3.11 or newer. macOS's built-in python3 is often 3.9, which is too old.
cd "$(dirname "$0")" || exit 1

ok_version() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

find_python() {
  for c in python3.13 python3.12 python3.11 \
           /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1 && ok_version "$c"; then echo "$c"; return 0; fi
  done
  return 1
}

fail() { echo; echo "$1"; echo; echo "Press Enter to close."; read -r; exit 1; }

# A previous attempt may have built the environment with an old Python: rebuild it.
if [ -x .venv/bin/python ] && ! ok_version .venv/bin/python; then
  echo "Removing an environment built with an old Python version..."
  rm -rf .venv
fi

if [ ! -x .venv/bin/python ]; then
  PY=$(find_python) || {
    xdg-open "https://www.python.org/downloads/" 2>/dev/null
    fail "Deal Hunter needs Python 3.11 or newer, and this computer only has $(python3 --version 2>&1).
I've opened python.org: download the latest 'Python for your Linux distribution', run it, then start Deal Hunter again."
  }
  echo "Setting up for the first time with $("$PY" --version)..."
  "$PY" -m venv .venv || fail "Could not create the Python environment."
  .venv/bin/python -m pip install --quiet --upgrade pip
fi

echo "Checking requirements..."
.venv/bin/python -m pip install --quiet -r requirements.txt || fail "Installing requirements failed (see the messages above). Check your internet connection and try again."

echo
echo "Deal Hunter is starting at http://127.0.0.1:8000  (keep this window open; close it to stop the app)"
(sleep 4; xdg-open http://127.0.0.1:8000) &
.venv/bin/python run.py
