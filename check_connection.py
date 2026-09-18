"""Standalone connection check -- run this any time you want a quick,
definitive answer to "is the app actually talking to Supabase right now?"
without opening the full dashboard.

Reads the same .env file the app uses, tries to connect, creates the
tables if they don't exist yet (harmless if they already do), and reports
how many campaigns it can see. Never prints your password.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

host = os.environ.get("SUPABASE_DB_HOST", "(not set)")
port = os.environ.get("SUPABASE_DB_PORT", "5432")
print(f"Testing connection to {host}:{port} ...")

try:
    import db

    db.init_db()
    campaigns = db.list_campaigns()
except Exception as exc:  # noqa: BLE001 -- this is a diagnostic script
    print(f"\nFAILED to connect: {exc}")
    sys.exit(1)

print(f"\nConnected successfully. {len(campaigns)} campaign(s) currently in the database:")
for c in campaigns:
    leads = db.get_leads(c["id"])
    print(f"  - {c['name']!r} ({c['status']}) -- {len(leads)} lead(s)")
if not campaigns:
    print("  (none yet -- that's expected if you haven't created one)")
