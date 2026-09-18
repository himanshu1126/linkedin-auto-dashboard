#!/bin/bash
# Double-click this to check whether the app can currently reach your
# Supabase database. Prints a clear pass/fail and waits so the window
# doesn't close before you can read it.
cd "$(dirname "$0")" || exit 1

if [ ! -d ".venv" ]; then
  echo "Setting up a local Python environment first (same as run.command)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
fi

echo "Syncing dependencies..."
if ! ./.venv/bin/pip install -r requirements.txt; then
  echo ""
  echo "Dependency install failed (see the error above) -- fix that first."
  echo "A common fix: delete the .venv folder next to this file and run this again."
  echo ""
  read -r -p "Press Enter to close this window..."
  exit 1
fi

./.venv/bin/python3 check_connection.py

echo ""
read -r -p "Press Enter to close this window..."
