"""The outreach state machine.

Every lead moves through a small set of statuses. Nothing in this file
ever touches LinkedIn -- it only decides what should happen next and
when, based on timestamps you (or the dashboard) record after you've
done the action yourself in your own browser.

Status flow
-----------
connect_pending  -> you send the connection request           -> connect_sent
connect_sent     -> they accept (you tell the app)             -> engage_pending
                                                                    (or straight to
                                                                     message_pending if
                                                                     the engagement step
                                                                     is turned off)
engage_pending   -> you like their recent post (or skip it)    -> message_pending
message_pending  -> you send message N                         -> waiting_reply
waiting_reply    -> gap_days[N-1] passes with no reply          -> message_pending (N+1)
                    or, if N was the last available message and
                    final_wait_days passes                      -> exhausted
(any non-terminal status) -> they reply (you tell the app)      -> replied   [terminal]
(any non-terminal status) -> you stop it manually                -> stopped  [terminal]

replied / exhausted / stopped are terminal: nothing more is ever surfaced
for that lead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import db

# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

CONNECT_PENDING = "connect_pending"
CONNECT_SENT = "connect_sent"
ENGAGE_PENDING = "engage_pending"
MESSAGE_PENDING = "message_pending"
WAITING_REPLY = "waiting_reply"
REPLIED = "replied"
EXHAUSTED = "exhausted"
STOPPED = "stopped"

TERMINAL_STATUSES = {REPLIED, EXHAUSTED, STOPPED}

STATUS_LABELS = {
    CONNECT_PENDING: "Ready to connect",
    CONNECT_SENT: "Awaiting acceptance",
    ENGAGE_PENDING: "Ready to engage",
    MESSAGE_PENDING: "Ready to message",
    WAITING_REPLY: "Waiting on reply window",
    REPLIED: "Replied \U0001F389",
    EXHAUSTED: "Sequence exhausted",
    STOPPED: "Stopped",
}

MAX_MESSAGES = 5


def default_settings() -> dict:
    return {
        # Days to wait after message N before message N+1 is due
        # (index 0 = after msg 1, ... index 3 = after msg 4).
        "gap_days": [3, 4, 5, 7],
        # Days to keep watching for a reply after the final message
        # before giving up on the lead.
        "final_wait_days": 7,
        # Whether the "like their recent post" step is part of the sequence.
        "use_engagement_step": True,
        # How many new connection requests the dashboard will surface per
        # calendar day. LinkedIn does not publish an official number; the
        # community-observed rolling weekly ceiling is roughly 100/week for
        # free & Premium accounts, so this default (15/day ≈ 105/week) sits
        # right at that edge -- treat it as a starting point, not a
        # guarantee, and pull it down if your acceptance rate is low.
        "daily_connection_cap": 15,
        # Purely informational: after this many days with no acceptance,
        # the dashboard flags the invite as stale so you can decide whether
        # to stop it. Nothing is auto-stopped.
        "stale_after_days": 21,
    }


def now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_since(ts: Optional[str]) -> Optional[float]:
    dt = _parse(ts)
    if dt is None:
        return None
    return (now() - dt).total_seconds() / 86400


# ---------------------------------------------------------------------------
# Transitions -- each returns nothing; they mutate the DB directly and log
# an activity_log entry so there's an audit trail.
# ---------------------------------------------------------------------------

def mark_connection_sent(lead: dict) -> None:
    db.update_lead(lead["id"], status=CONNECT_SENT, connect_sent_at=db.now_iso())
    db.log_activity(lead["id"], lead["campaign_id"], "connection_sent")


def mark_connection_accepted(lead: dict, settings: dict) -> None:
    next_status = ENGAGE_PENDING if settings.get("use_engagement_step", True) else MESSAGE_PENDING
    db.update_lead(lead["id"], status=next_status, connect_accepted_at=db.now_iso())
    db.log_activity(lead["id"], lead["campaign_id"], "connection_accepted")


def mark_engaged(lead: dict, liked: bool) -> None:
    db.update_lead(lead["id"], status=MESSAGE_PENDING, engaged_at=db.now_iso())
    db.log_activity(lead["id"], lead["campaign_id"], "engaged" if liked else "engagement_skipped")


def mark_message_sent(lead: dict) -> None:
    new_index = lead["message_index"] + 1
    db.update_lead(
        lead["id"],
        status=WAITING_REPLY,
        message_index=new_index,
        last_message_sent_at=db.now_iso(),
    )
    db.log_activity(lead["id"], lead["campaign_id"], f"message_{new_index}_sent")


def mark_replied(lead: dict, note: str = "") -> None:
    db.update_lead(lead["id"], status=REPLIED, replied_at=db.now_iso(), reply_note=note)
    db.log_activity(lead["id"], lead["campaign_id"], "replied", note or None)


def mark_stopped(lead: dict, reason: str = "") -> None:
    db.update_lead(lead["id"], status=STOPPED, stopped_reason=reason)
    db.log_activity(lead["id"], lead["campaign_id"], "stopped", reason or None)


def reopen_lead(lead: dict) -> None:
    """Undo a manual stop, sending the lead back to wherever it was before
    (best-effort: just resumes at the pending action implied by what's
    already recorded)."""
    if lead["message_index"] > 0:
        status = WAITING_REPLY
    elif lead["engaged_at"]:
        status = MESSAGE_PENDING
    elif lead["connect_accepted_at"]:
        status = ENGAGE_PENDING
    elif lead["connect_sent_at"]:
        status = CONNECT_SENT
    else:
        status = CONNECT_PENDING
    db.update_lead(lead["id"], status=status, stopped_reason=None)
    db.log_activity(lead["id"], lead["campaign_id"], "reopened")


# ---------------------------------------------------------------------------
# Time-based recompute: promotes waiting_reply -> message_pending / exhausted
# ---------------------------------------------------------------------------

def recompute_campaign(campaign_id: int, settings: dict) -> int:
    """Call this whenever the dashboard loads. Returns how many leads moved."""
    moved = 0
    for lead in db.get_leads(campaign_id, status=WAITING_REPLY):
        if _recompute_lead(lead, settings):
            moved += 1
    return moved


def _recompute_lead(lead: dict, settings: dict) -> bool:
    elapsed = days_since(lead["last_message_sent_at"])
    if elapsed is None:
        return False
    n_sent = lead["message_index"]
    n_available = min(MAX_MESSAGES, len(lead["messages"]))
    gap_days = settings.get("gap_days", default_settings()["gap_days"])
    final_wait = settings.get("final_wait_days", default_settings()["final_wait_days"])

    if n_sent >= n_available:
        # That was the last message we have for this lead -- watch a bit
        # longer, then give up.
        if elapsed >= final_wait:
            db.update_lead(lead["id"], status=EXHAUSTED)
            db.log_activity(lead["id"], lead["campaign_id"], "exhausted")
            return True
        return False

    gap = gap_days[min(n_sent - 1, len(gap_days) - 1)] if n_sent >= 1 else 0
    if elapsed >= gap:
        db.update_lead(lead["id"], status=MESSAGE_PENDING)
        db.log_activity(lead["id"], lead["campaign_id"], f"message_{n_sent + 1}_due")
        return True
    return False


# ---------------------------------------------------------------------------
# Queue helpers for the dashboard
# ---------------------------------------------------------------------------

def today_connection_queue(campaign_id: int, settings: dict) -> list[dict]:
    cap = settings.get("daily_connection_cap", default_settings()["daily_connection_cap"])
    sent_today = db.count_action_today(campaign_id, "connection_sent")
    remaining = max(0, cap - sent_today)
    if remaining == 0:
        return []
    pending = db.get_leads(campaign_id, status=CONNECT_PENDING)
    return pending[:remaining]


def connection_cap_status(campaign_id: int, settings: dict) -> tuple[int, int]:
    """Returns (sent_today, cap)."""
    cap = settings.get("daily_connection_cap", default_settings()["daily_connection_cap"])
    sent_today = db.count_action_today(campaign_id, "connection_sent")
    return sent_today, cap


def next_message_number(lead: dict) -> int:
    """1-based number of the message that is due next."""
    return lead["message_index"] + 1


def next_message_text(lead: dict) -> Optional[str]:
    idx = lead["message_index"]
    if idx < len(lead["messages"]):
        return lead["messages"][idx]
    return None


def is_stale(lead: dict, settings: dict) -> bool:
    if lead["status"] != CONNECT_SENT:
        return False
    d = days_since(lead["connect_sent_at"])
    threshold = settings.get("stale_after_days", default_settings()["stale_after_days"])
    return d is not None and d >= threshold


def funnel_counts(campaign_id: int) -> dict:
    all_statuses = [
        CONNECT_PENDING,
        CONNECT_SENT,
        ENGAGE_PENDING,
        MESSAGE_PENDING,
        WAITING_REPLY,
        REPLIED,
        EXHAUSTED,
        STOPPED,
    ]
    leads = db.get_leads(campaign_id)
    counts = {s: 0 for s in all_statuses}
    for lead in leads:
        counts[lead["status"]] = counts.get(lead["status"], 0) + 1
    counts["_total"] = len(leads)
    return counts


def active_leads(campaign_id: int) -> list[dict]:
    """All leads not yet in a terminal state -- used for the campaign-wide
    'mark someone as replied right now' picker."""
    return [l for l in db.get_leads(campaign_id) if l["status"] not in TERMINAL_STATUSES]
