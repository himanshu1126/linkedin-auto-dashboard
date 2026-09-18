#!/bin/bash
# Double-click this file in Finder to set up (first run only) and launch
# the LinkedIn Outreach Tracker. If macOS blocks it as "from an
# unidentified developer", right-click -> Open once to allow it.
cd "$(dirname "$0")" || exit 1

if [ ! -d ".venv" ]; then
  echo "First run: setting up a local Python environment..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
fi

# Always sync dependencies (cheap no-op if nothing changed) so an update
# that adds a package -- like the Supabase driver -- doesn't need a
# manual reinstall.
if ! ./.venv/bin/pip install -r requirements.txt; then
  echo ""
  echo "Dependency install failed (see the error above) -- fix that first."
  echo "A common fix: delete the .venv folder next to this file and run this again."
  read -r -p "Press Enter to close this window..."
  exit 1
fi

echo "Starting the LinkedIn Outreach Tracker -- this will open in your browser."
./.venv/bin/streamlit run app.py
