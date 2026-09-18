"""PostgreSQL (Supabase) data layer for the LinkedIn Outreach Tracker.

Important: this module only ever stores data in your own Supabase
project. It has no LinkedIn-facing network code -- every LinkedIn action
(the connection request, the like, each message) is something you do
yourself, in your own logged-in browser. This module just remembers
where every lead is in the sequence so you don't have to.

Connection settings come from environment variables (loaded from a local
.env file that is never committed or sent anywhere):

    SUPABASE_DB_HOST      e.g. db.xxxxxxxxxxxx.supabase.co
    SUPABASE_DB_PORT      default 5432
    SUPABASE_DB_NAME      default postgres
    SUPABASE_DB_USER      default postgres
    SUPABASE_DB_PASSWORD  required

If your network can't reach the direct db.<ref>.supabase.co host (it's
IPv6-only, and most home/office networks are IPv4-only), grab the
"Session pooler" connection details from Supabase's dashboard
(Project Settings -> Database -> Connection string) instead -- same
variables, different host/port/user (the pooler user is
`postgres.<project-ref>` rather than plain `postgres`).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv
from psycopg2.extras import Json, RealDictCursor, execute_values

load_dotenv()

# Columns on `leads` that callers are allowed to update via update_lead().
# Kept as an explicit allow-list so update_lead can build SQL safely.
LEAD_UPDATABLE_FIELDS = {
    "status",
    "message_index",
    "connect_sent_at",
    "connect_accepted_at",
    "engaged_at",
    "last_message_sent_at",
    "replied_at",
    "reply_note",
    "stopped_reason",
    "updated_at",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Connection pool
# --------------------------------------------------------------------------

_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None


def _connect_kwargs() -> dict:
    password = os.environ.get("SUPABASE_DB_PASSWORD")
    if not password:
        raise RuntimeError(
            "SUPABASE_DB_PASSWORD is not set. Create a .env file next to this "
            "app (see README.md) with SUPABASE_DB_HOST/PORT/NAME/USER/PASSWORD."
        )
    return dict(
        host=os.environ.get("SUPABASE_DB_HOST", "").strip(),
        port=os.environ.get("SUPABASE_DB_PORT", "5432").strip(),
        dbname=os.environ.get("SUPABASE_DB_NAME", "postgres").strip(),
        user=os.environ.get("SUPABASE_DB_USER", "postgres").strip(),
        password=password,
        sslmode=os.environ.get("SUPABASE_DB_SSLMODE", "require").strip(),
        cursor_factory=RealDictCursor,
        connect_timeout=10,
    )


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        kwargs = _connect_kwargs()
        if not kwargs["host"]:
            raise RuntimeError(
                "SUPABASE_DB_HOST is not set. Create a .env file next to this "
                "app (see README.md) with your Supabase connection details."
            )
        try:
            _pool = psycopg2.pool.SimpleConnectionPool(1, 5, **kwargs)
        except psycopg2.OperationalError as exc:
            raise RuntimeError(
                f"Couldn't connect to Supabase at {kwargs['host']}:{kwargs['port']} -- "
                "double check your .env values. If your network is IPv4-only, the direct "
                "db.<ref>.supabase.co host may not be reachable (it's IPv6); use the "
                "'Session pooler' host/port from Supabase's dashboard instead. "
                f"Original error: {exc}"
            ) from exc
    return _pool


@contextmanager
def get_conn() -> Iterator[Any]:
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS campaigns (
                    id             SERIAL PRIMARY KEY,
                    name           TEXT NOT NULL,
                    created_at     TEXT NOT NULL,
                    status         TEXT NOT NULL DEFAULT 'active',
                    column_mapping JSONB NOT NULL,
                    settings       JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id                    SERIAL PRIMARY KEY,
                    campaign_id           INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    row_index             INTEGER,
                    profile_url           TEXT NOT NULL,
                    display_name          TEXT,
                    company               TEXT,
                    raw_data              JSONB NOT NULL,
                    messages              JSONB NOT NULL,
                    status                TEXT NOT NULL DEFAULT 'connect_pending',
                    message_index         INTEGER NOT NULL DEFAULT 0,
                    connect_sent_at       TEXT,
                    connect_accepted_at   TEXT,
                    engaged_at            TEXT,
                    last_message_sent_at  TEXT,
                    replied_at            TEXT,
                    reply_note            TEXT,
                    stopped_reason        TEXT,
                    created_at            TEXT NOT NULL,
                    updated_at            TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity_log (
                    id           SERIAL PRIMARY KEY,
                    lead_id      INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                    campaign_id  INTEGER NOT NULL,
                    timestamp    TEXT NOT NULL,
                    action       TEXT NOT NULL,
                    note         TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_leads_campaign_status ON leads(campaign_id, status);
                CREATE INDEX IF NOT EXISTS idx_activity_lead ON activity_log(lead_id);
                CREATE INDEX IF NOT EXISTS idx_activity_campaign_time ON activity_log(campaign_id, timestamp);
                """
            )


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------

def create_campaign(name: str, column_mapping: dict, settings: dict) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO campaigns (name, created_at, status, column_mapping, settings) "
                "VALUES (%s, %s, 'active', %s, %s) RETURNING id",
                (name, now_iso(), Json(column_mapping), Json(settings)),
            )
            return cur.fetchone()["id"]


def list_campaigns() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM campaigns ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]


def get_campaign(campaign_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM campaigns WHERE id = %s", (campaign_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def update_campaign_status(campaign_id: int, status: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE campaigns SET status = %s WHERE id = %s", (status, campaign_id))


def update_campaign_settings(campaign_id: int, settings: dict) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE campaigns SET settings = %s WHERE id = %s",
                (Json(settings), campaign_id),
            )


def delete_campaign(campaign_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM campaigns WHERE id = %s", (campaign_id,))


# --------------------------------------------------------------------------
# Leads
# --------------------------------------------------------------------------

def create_leads_bulk(campaign_id: int, leads: list[dict]) -> None:
    """leads: list of dicts with keys row_index, profile_url, display_name,
    company, raw_data (dict), messages (list[str])."""
    if not leads:
        return
    ts = now_iso()
    rows = [
        (
            campaign_id,
            lead.get("row_index"),
            lead["profile_url"],
            lead.get("display_name"),
            lead.get("company"),
            Json(lead.get("raw_data", {})),
            Json(lead.get("messages", [])),
            "connect_pending",
            0,
            ts,
            ts,
        )
        for lead in leads
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO leads (
                    campaign_id, row_index, profile_url, display_name, company,
                    raw_data, messages, status, message_index, created_at, updated_at
                ) VALUES %s
                """,
                rows,
            )


def get_leads(campaign_id: int, status: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT * FROM leads WHERE campaign_id = %s AND status = %s ORDER BY row_index",
                    (campaign_id, status),
                )
            else:
                cur.execute(
                    "SELECT * FROM leads WHERE campaign_id = %s ORDER BY row_index",
                    (campaign_id,),
                )
            return [dict(r) for r in cur.fetchall()]


def get_lead(lead_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id = %s", (lead_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def update_lead(lead_id: int, **fields: Any) -> None:
    bad = set(fields) - LEAD_UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"Not updatable via update_lead: {bad}")
    fields["updated_at"] = now_iso()
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE leads SET {set_clause} WHERE id = %s",
                (*fields.values(), lead_id),
            )


def delete_lead(lead_id: int) -> None:
    """Permanently removes a lead and (via ON DELETE CASCADE) its activity
    log. Irreversible -- use update_lead(status='stopped', ...) instead if
    you just want to end a sequence but keep the record."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM leads WHERE id = %s", (lead_id,))


def delete_leads(lead_ids: list[int]) -> None:
    if not lead_ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM leads WHERE id = ANY(%s)", (list(lead_ids),))


# --------------------------------------------------------------------------
# Activity log
# --------------------------------------------------------------------------

def log_activity(lead_id: int, campaign_id: int, action: str, note: Optional[str] = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO activity_log (lead_id, campaign_id, timestamp, action, note) "
                "VALUES (%s, %s, %s, %s, %s)",
                (lead_id, campaign_id, now_iso(), action, note),
            )


def recent_activity(campaign_id: int, limit: int = 25) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT al.*, l.display_name, l.company
                FROM activity_log al
                JOIN leads l ON l.id = al.lead_id
                WHERE al.campaign_id = %s
                ORDER BY al.timestamp DESC
                LIMIT %s
                """,
                (campaign_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]


def count_action_today(campaign_id: int, action: str) -> int:
    """How many times `action` was logged today (local calendar date)."""
    today = datetime.now().date().isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT timestamp FROM activity_log WHERE campaign_id = %s AND action = %s",
                (campaign_id, action),
            )
            rows = cur.fetchall()
    count = 0
    for r in rows:
        ts = r["timestamp"]
        try:
            local_date = datetime.fromisoformat(ts).astimezone().date().isoformat()
        except ValueError:
            local_date = ts[:10]
        if local_date == today:
            count += 1
    return count
