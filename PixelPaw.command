#!/bin/sh
# 🐾 PixelPaw for macOS / Linux / Raspberry Pi / Termux: double-click (macOS) or run once.
# The first run installs everything (about a minute), then PixelPaw runs in the background.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "PixelPaw: first-time setup, this takes about a minute..."
  PY=$(command -v python3 || command -v python)
  if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "PixelPaw needs Python 3.10 or newer: https://www.python.org/downloads/"
    exit 1
  fi
  "$PY" -m venv .venv && .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || {
    echo "Setup failed. Check your internet connection and try again."; rm -rf .venv; exit 1; }
fi

nohup .venv/bin/python PixelPaw.pyw "$@" >/dev/null 2>&1 &
echo "PixelPaw is running. Dashboard: http://127.0.0.1:8765/"
