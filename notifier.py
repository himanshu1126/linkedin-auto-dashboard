"""Desktop notifications, with no heavyweight dependencies.

Uses each OS's native notifier via subprocess (osascript on macOS,
notify-send on Linux). On Windows it tries `plyer` if installed, and
silently no-ops if not -- a missing notifier should never crash the app.

This file is also directly runnable:

    python notifier.py

which checks every active campaign for anything due (or gone stale) and
fires one summary notification per campaign. Run it by hand, or add it to
cron / Task Scheduler / launchd if you want a nudge even when the
Streamlit dashboard isn't open. It never opens a browser or touches
LinkedIn -- it only reads the local database this app already keeps.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATE_PATH = Path(__file__).resolve().parent / "data" / "notifier_state.json"
MIN_RENOTIFY_HOURS = 3


def notify(title: str, message: str) -> bool:
    """Best-effort desktop notification. Returns True if one was fired."""
    try:
        if sys.platform == "darwin":
            safe_title = title.replace('"', '\\"')
            safe_msg = message.replace('"', '\\"')
            result = subprocess.run(
                ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
                check=False,
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0

        if sys.platform.startswith("linux"):
            if shutil.which("notify-send"):
                result = subprocess.run(
                    ["notify-send", title, message], check=False, timeout=5
                )
                return result.returncode == 0
            return False

        if sys.platform == "win32":
            try:
                from plyer import notification as plyer_notification  # optional dep

                plyer_notification.notify(title=title, message=message, timeout=5)
                return True
            except Exception:
                return False
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Standalone periodic check (importable too, e.g. from app.py on page load)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def _hours_since(iso_ts: Optional[str]) -> float:
    if not iso_ts:
        return 1e9
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return 1e9
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def check_and_notify_all(force: bool = False) -> list[str]:
    """Recomputes every active campaign, then fires at most one notification
    per campaign (rate-limited) summarizing what's due. Returns the list of
    messages that were sent (useful for logging / the standalone script)."""
    import db
    import sequence_engine as engine

    db.init_db()
    state = _load_state()
    fired: list[str] = []

    for campaign in db.list_campaigns():
        if campaign["status"] != "active":
            continue
        settings = campaign["settings"]
        engine.recompute_campaign(campaign["id"], settings)

        counts = engine.funnel_counts(campaign["id"])
        due = (
            len(engine.today_connection_queue(campaign["id"], settings))
            + counts.get(engine.ENGAGE_PENDING, 0)
            + counts.get(engine.MESSAGE_PENDING, 0)
        )
        stale = sum(
            1
            for lead in db.get_leads(campaign["id"], status=engine.CONNECT_SENT)
            if engine.is_stale(lead, settings)
        )

        key = str(campaign["id"])
        last = state.get(key, {})
        hours_since_last = _hours_since(last.get("notified_at"))

        if due == 0:
            continue
        if not force and hours_since_last < MIN_RENOTIFY_HOURS:
            continue

        parts = [f"{due} action(s) ready"]
        if stale:
            parts.append(f"{stale} invite(s) gone quiet")
        msg = f"{campaign['name']}: " + ", ".join(parts)
        if notify("LinkedIn Outreach Tracker", msg):
            fired.append(msg)
        state[key] = {"notified_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    _save_state(state)
    return fired


if __name__ == "__main__":
    messages = check_and_notify_all(force="--force" in sys.argv)
    if messages:
        for m in messages:
            print(f"Notified: {m}")
    else:
        print("Nothing due right now.")
