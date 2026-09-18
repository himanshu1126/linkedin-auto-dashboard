"""LinkedIn Outreach Tracker -- Streamlit UI.

This app tracks a personalized, multi-step outreach sequence built from
an Excel/CSV sheet. It deliberately does NOT log into LinkedIn or click
anything there on its own: every connection request, every like, every
message is something you do yourself, in your own browser. The app's job
is only to remember who's next, keep your pacing sane, hold the right
message text ready to paste, and stop + alert you the moment someone
replies.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import db
import excel_io
import notifier
import sequence_engine as engine

st.set_page_config(page_title="LinkedIn Outreach Tracker", page_icon="\U0001F517", layout="wide")
db.init_db()

NAV_OPTIONS = ["Upload & Map", "Dashboard", "All Leads", "Settings", "Help & Safety"]


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _lead_label(lead: dict) -> str:
    label = lead["display_name"] or lead["profile_url"]
    if lead["company"]:
        label += f" — {lead['company']}"
    return label


def _lead_option_label(lead: dict) -> str:
    """Label for picker widgets -- includes the id so two same-named leads
    never collide as dict keys."""
    status = engine.STATUS_LABELS.get(lead["status"], lead["status"])
    return f"{_lead_label(lead)} [{status}] (#{lead['id']})"


def _settings_widgets(current: dict, key_prefix: str) -> dict:
    """Renders the sequencing-settings widgets and returns a settings dict.
    Shared between the Upload page (new campaign) and Settings page (edit)."""
    c1, c2, c3 = st.columns(3)
    with c1:
        use_engagement = st.checkbox(
            "Like their recent post before messaging",
            value=current.get("use_engagement_step", True),
            key=f"{key_prefix}_use_engagement",
        )
    with c2:
        daily_cap = st.number_input(
            "New connection requests to surface per day",
            min_value=1,
            max_value=200,
            value=int(current.get("daily_connection_cap", 15)),
            key=f"{key_prefix}_daily_cap",
            help=(
                "LinkedIn doesn't publish an official number, but a rolling ~100/week "
                "ceiling for free & Premium accounts is widely and consistently observed "
                "(more on Sales Navigator). 15/day keeps you near that edge on a 7-day "
                "rolling basis -- pull it down if your acceptance rate is low, and check "
                "LinkedIn's own behavior for your account since this isn't official."
            ),
        )
    with c3:
        stale_after = st.number_input(
            "Flag an invite as 'gone quiet' after (days)",
            min_value=1,
            max_value=90,
            value=int(current.get("stale_after_days", 21)),
            key=f"{key_prefix}_stale_after",
        )

    g1, g2 = st.columns(2)
    with g1:
        uniform_gap = st.number_input(
            "Days to wait after a message before the next one is due",
            min_value=1,
            max_value=60,
            value=int(current.get("gap_days", [4])[0]),
            key=f"{key_prefix}_uniform_gap",
        )
    with g2:
        final_wait = st.number_input(
            "Days to keep watching for a reply after the final message",
            min_value=1,
            max_value=60,
            value=int(current.get("final_wait_days", 7)),
            key=f"{key_prefix}_final_wait",
        )

    gap_days = [uniform_gap] * 4
    customize = st.checkbox(
        "Customize the wait after each individual message",
        key=f"{key_prefix}_customize_gaps",
    )
    if customize:
        existing = current.get("gap_days", [uniform_gap] * 4)
        cols = st.columns(4)
        step_labels = ["After message 1", "After message 2", "After message 3", "After message 4"]
        for i, col in enumerate(cols):
            with col:
                gap_days[i] = st.number_input(
                    step_labels[i],
                    min_value=1,
                    max_value=60,
                    value=int(existing[i]) if i < len(existing) else uniform_gap,
                    key=f"{key_prefix}_gap_{i}",
                )

    return {
        "use_engagement_step": use_engagement,
        "daily_connection_cap": daily_cap,
        "stale_after_days": stale_after,
        "gap_days": gap_days,
        "final_wait_days": final_wait,
    }


# ---------------------------------------------------------------------------
# Page: Upload & Map
# ---------------------------------------------------------------------------

def page_upload() -> None:
    st.header("Upload & map a new campaign")
    st.caption(
        "Upload your leads sheet, tell the app which column is the LinkedIn profile "
        "URL and which columns hold your messages, and it builds a tracked sequence "
        "you work through by hand."
    )

    uploaded = st.file_uploader("Leads spreadsheet (.xlsx, .xls or .csv)", type=["xlsx", "xls", "csv"])
    if not uploaded:
        st.info("No file yet. Need an example first? See the sample sheet in the project folder.")
        return

    try:
        df = excel_io.load_table_from_upload(uploaded)
    except Exception as exc:  # noqa: BLE001 -- surface any parse error to the user
        st.error(f"Couldn't read that file: {exc}")
        return

    if df.empty:
        st.warning("That sheet looks empty.")
        return

    st.write(f"**{len(df)} rows** found. Preview:")
    st.dataframe(df.head(10), width="stretch")

    st.subheader("Map your columns")
    options = [""] + list(df.columns)
    mc1, mc2 = st.columns(2)
    with mc1:
        profile_col = st.selectbox("LinkedIn profile URL column *", options, key="map_profile")
        name_col = st.selectbox("Name column (optional, for display)", options, key="map_name")
        company_col = st.selectbox("Company column (optional)", options, key="map_company")
    with mc2:
        st.caption("Message columns — map as many as you use. Values may contain {ColumnName} placeholders.")
        msg_cols = {}
        for i in range(1, engine.MAX_MESSAGES + 1):
            required_mark = " *" if i == 1 else " (optional)"
            msg_cols[i] = st.selectbox(f"Message {i} column{required_mark}", options, key=f"map_msg_{i}")

    mapping = {"profile_url": profile_col, "name": name_col, "company": company_col}
    mapping.update({f"message_{i}": msg_cols[i] for i in range(1, engine.MAX_MESSAGES + 1)})

    for w in excel_io.validate_mapping(df, mapping):
        st.warning(w)

    st.subheader("Sequencing settings")
    settings = _settings_widgets(engine.default_settings(), key_prefix="new")

    st.subheader("Campaign name")
    campaign_name = st.text_input("Name", value="New campaign", label_visibility="collapsed")

    can_create = bool(profile_col) and bool(msg_cols[1])
    if st.button("Create campaign", type="primary", disabled=not can_create):
        leads = excel_io.build_leads_from_df(df, mapping)
        if not leads:
            st.error("No valid rows found -- check the profile URL column mapping.")
        else:
            campaign_id = db.create_campaign(campaign_name or "Untitled campaign", mapping, settings)
            db.create_leads_bulk(campaign_id, leads)
            st.session_state.selected_campaign_id = campaign_id
            st.session_state.nav = "Dashboard"
            st.success(f"Created '{campaign_name}' with {len(leads)} leads.")
            st.rerun()
    elif not can_create:
        st.caption("Map at least the profile URL and Message 1 columns to continue.")


# ---------------------------------------------------------------------------
# Page: Dashboard
# ---------------------------------------------------------------------------

def page_dashboard(campaign: dict) -> None:
    settings = campaign["settings"]
    engine.recompute_campaign(campaign["id"], settings)

    top = st.columns([4, 1, 1])
    with top[0]:
        st.header(campaign["name"])
        st.caption(f"Status: {campaign['status']}")
    with top[1]:
        if campaign["status"] == "active":
            if st.button("⏸ Pause", width="stretch"):
                db.update_campaign_status(campaign["id"], "paused")
                st.rerun()
        else:
            if st.button("▶ Resume", width="stretch"):
                db.update_campaign_status(campaign["id"], "active")
                st.rerun()
    with top[2]:
        if st.button("\U0001F5C4 Archive", width="stretch"):
            db.update_campaign_status(campaign["id"], "archived")
            st.rerun()

    counts = engine.funnel_counts(campaign["id"])
    metric_defs = [
        (engine.CONNECT_PENDING, "New"),
        (engine.CONNECT_SENT, "Awaiting accept"),
        (engine.ENGAGE_PENDING, "To engage"),
        (engine.MESSAGE_PENDING, "To message"),
        (engine.WAITING_REPLY, "Waiting"),
        (engine.REPLIED, "Replied"),
        (engine.EXHAUSTED, "Exhausted"),
    ]
    metric_cols = st.columns(len(metric_defs))
    for col, (status_key, label) in zip(metric_cols, metric_defs):
        col.metric(label, counts.get(status_key, 0))

    if campaign["status"] != "active":
        st.info("This campaign is paused, so no actions are being surfaced below. Resume it to continue working the queue.")
        return

    st.divider()
    st.subheader("Quick actions")
    qa1, qa2 = st.columns(2)

    with qa1:
        st.markdown("**\U0001F514 Someone replied?**")
        st.caption("Mark it the moment you see it — no need to wait for their scheduled step.")
        active = engine.active_leads(campaign["id"])
        if active:
            reply_map = {_lead_option_label(l): l["id"] for l in active}
            picked = st.selectbox("Who replied?", [""] + list(reply_map.keys()), key="reply_picker")
            if picked:
                note = st.text_input("Optional note", key="reply_note_input")
                if st.button("Mark replied & stop their sequence", type="primary", key="reply_confirm_btn"):
                    lead = db.get_lead(reply_map[picked])
                    engine.mark_replied(lead, note)
                    notifier.notify(
                        "LinkedIn Outreach Tracker",
                        f"{_lead_label(lead)} replied — sequence stopped.",
                    )
                    st.success(f"{_lead_label(lead)} marked as replied. Sequence stopped.")
                    st.rerun()
        else:
            st.caption("No active leads yet — nothing to mark.")

    with qa2:
        st.markdown("**\U0001F5D1️ Remove a lead**")
        st.caption("Permanently deletes them from this campaign. Use 'Stop' in the queue below instead if you just want to end their sequence but keep the record.")
        everyone = db.get_leads(campaign["id"])
        if everyone:
            delete_map = {_lead_option_label(l): l["id"] for l in everyone}
            del_picked = st.selectbox("Who do you want to remove?", [""] + list(delete_map.keys()), key="delete_picker")
            if del_picked:
                del_id = delete_map[del_picked]
                confirm_key = f"confirm_delete_lead_{del_id}"
                if st.button("Delete this lead", key="delete_lead_btn"):
                    st.session_state[confirm_key] = True
                if st.session_state.get(confirm_key):
                    st.warning("This can't be undone — it removes them and their activity history.")
                    if st.button("Yes, delete permanently", type="primary", key="delete_lead_confirm_btn"):
                        db.delete_lead(del_id)
                        st.session_state[confirm_key] = False
                        st.success("Deleted.")
                        st.rerun()
        else:
            st.caption("No leads in this campaign yet.")

    st.divider()
    st.subheader("Today's queue")
    _render_connection_queue(campaign, settings)
    _render_awaiting_acceptance(campaign, settings)
    if settings.get("use_engagement_step", True):
        _render_engagement_queue(campaign)
    _render_message_queue(campaign)
    _render_waiting_reply(campaign, settings)

    st.divider()
    with st.expander("Recent activity"):
        entries = db.recent_activity(campaign["id"], limit=20)
        if not entries:
            st.caption("Nothing logged yet.")
        for e in entries:
            when = e["timestamp"][:16].replace("T", " ")
            who = e["display_name"] or "lead"
            extra = f" — {e['note']}" if e["note"] else ""
            st.caption(f"{when} · {who}: {e['action']}{extra}")


def _render_connection_queue(campaign: dict, settings: dict) -> None:
    queue = engine.today_connection_queue(campaign["id"], settings)
    sent_today, cap = engine.connection_cap_status(campaign["id"], settings)
    st.markdown(f"**1. Send connection requests** — {sent_today}/{cap} sent today")
    if not queue:
        st.caption("Nothing due (either you're caught up, or today's cap is reached — come back tomorrow).")
        return
    for lead in queue:
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 1, 1])
            with c1:
                st.write(f"**{_lead_label(lead)}**")
                st.caption(lead["profile_url"])
            with c2:
                st.link_button("Open profile ↗", lead["profile_url"], width="stretch")
            with c3:
                if st.button("Mark sent", key=f"conn_sent_{lead['id']}", width="stretch"):
                    engine.mark_connection_sent(lead)
                    st.rerun()
            if st.button("Not a fit — skip", key=f"conn_skip_{lead['id']}"):
                engine.mark_stopped(lead, "Skipped before connecting")
                st.rerun()


def _render_awaiting_acceptance(campaign: dict, settings: dict) -> None:
    leads = db.get_leads(campaign["id"], status=engine.CONNECT_SENT)
    st.markdown(f"**Awaiting acceptance** ({len(leads)})")
    if not leads:
        st.caption("Nobody waiting on an invite right now.")
        return
    for lead in leads:
        stale = engine.is_stale(lead, settings)
        d = engine.days_since(lead["connect_sent_at"]) or 0
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            with c1:
                st.write(f"**{_lead_label(lead)}**")
                quiet = "  ⚠️ gone quiet" if stale else ""
                st.caption(f"Invited {d:.0f} day(s) ago{quiet}")
            with c2:
                st.link_button("Open profile ↗", lead["profile_url"], width="stretch")
            with c3:
                if st.button("Accepted ✅", key=f"acc_{lead['id']}", width="stretch"):
                    engine.mark_connection_accepted(lead, settings)
                    st.rerun()
            with c4:
                if st.button("No response", key=f"stale_{lead['id']}", width="stretch"):
                    engine.mark_stopped(lead, "No response to connection request")
                    st.rerun()


def _render_engagement_queue(campaign: dict) -> None:
    leads = db.get_leads(campaign["id"], status=engine.ENGAGE_PENDING)
    st.markdown(f"**2. Like their most recent post** ({len(leads)})")
    if not leads:
        st.caption("Nothing to engage with right now.")
        return
    for lead in leads:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            with c1:
                st.write(f"**{_lead_label(lead)}**")
                st.caption("Open their profile, like whatever's most recent, then mark it here.")
            with c2:
                st.link_button("Open profile ↗", lead["profile_url"], width="stretch")
            with c3:
                if st.button("Liked ✅", key=f"like_{lead['id']}", width="stretch"):
                    engine.mark_engaged(lead, liked=True)
                    st.rerun()
            with c4:
                if st.button("Skip step", key=f"like_skip_{lead['id']}", width="stretch"):
                    engine.mark_engaged(lead, liked=False)
                    st.rerun()


def _render_message_queue(campaign: dict) -> None:
    leads = db.get_leads(campaign["id"], status=engine.MESSAGE_PENDING)
    st.markdown(f"**3. Send the next message** ({len(leads)})")
    if not leads:
        st.caption("No messages due right now.")
        return
    for lead in leads:
        n = engine.next_message_number(lead)
        text = engine.next_message_text(lead) or "(no message text found for this step — check your column mapping)"
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.write(f"**{_lead_label(lead)}** — message {n}")
            with c2:
                st.link_button("Open profile ↗", lead["profile_url"], width="stretch")
            st.code(text, language=None)
            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button("Mark sent", key=f"msg_sent_{lead['id']}", width="stretch"):
                    engine.mark_message_sent(lead)
                    st.rerun()
            with b2:
                if st.button("Already replied", key=f"msg_reply_{lead['id']}", width="stretch"):
                    engine.mark_replied(lead)
                    notifier.notify("LinkedIn Outreach Tracker", f"{_lead_label(lead)} replied — sequence stopped.")
                    st.rerun()
            with b3:
                if st.button("Stop sequence", key=f"msg_stop_{lead['id']}", width="stretch"):
                    engine.mark_stopped(lead, "Manually stopped")
                    st.rerun()


def _render_waiting_reply(campaign: dict, settings: dict) -> None:
    leads = db.get_leads(campaign["id"], status=engine.WAITING_REPLY)
    st.markdown(f"**Waiting on reply window** ({len(leads)})")
    if not leads:
        st.caption("Nobody in a wait window right now.")
        return
    gaps = settings.get("gap_days", [3, 4, 5, 7])
    final_wait = settings.get("final_wait_days", 7)
    for lead in leads:
        elapsed = engine.days_since(lead["last_message_sent_at"]) or 0
        n_available = min(engine.MAX_MESSAGES, len(lead["messages"]))
        if lead["message_index"] >= n_available:
            note = f"final wait window — {elapsed:.1f}/{final_wait} days"
        else:
            gap = gaps[min(lead["message_index"] - 1, len(gaps) - 1)]
            note = f"next message in ~{max(gap - elapsed, 0):.1f} day(s)"
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"{_lead_label(lead)} — sent {lead['message_index']} message(s), {note}")
            with c2:
                if st.button("They replied", key=f"wait_reply_{lead['id']}", width="stretch"):
                    engine.mark_replied(lead)
                    notifier.notify("LinkedIn Outreach Tracker", f"{_lead_label(lead)} replied — sequence stopped.")
                    st.rerun()


# ---------------------------------------------------------------------------
# Page: All Leads
# ---------------------------------------------------------------------------

def page_all_leads(campaign: dict) -> None:
    st.header(f"All leads — {campaign['name']}")
    leads = db.get_leads(campaign["id"])
    if not leads:
        st.info("No leads in this campaign yet.")
        return

    rows = [
        {
            "Name": l["display_name"],
            "Company": l["company"],
            "Profile": l["profile_url"],
            "Status": engine.STATUS_LABELS.get(l["status"], l["status"]),
            "Messages sent": l["message_index"],
            "Replied": "Yes" if l["replied_at"] else "",
            "Stopped reason": l["stopped_reason"] or "",
            "Updated": l["updated_at"][:16].replace("T", " "),
        }
        for l in leads
    ]
    table = pd.DataFrame(rows)

    f1, f2 = st.columns([1, 2])
    with f1:
        status_filter = st.multiselect("Filter by status", sorted(table["Status"].unique()))
    with f2:
        search = st.text_input("Search name / company")

    filtered = table
    if status_filter:
        filtered = filtered[filtered["Status"].isin(status_filter)]
    if search:
        mask = filtered["Name"].str.contains(search, case=False, na=False) | filtered["Company"].str.contains(
            search, case=False, na=False
        )
        filtered = filtered[mask]

    st.dataframe(filtered, width="stretch", hide_index=True)
    st.download_button(
        "Export this view as CSV",
        filtered.to_csv(index=False).encode("utf-8"),
        file_name=f"{campaign['name']}_leads.csv",
        mime="text/csv",
    )

    st.divider()
    st.subheader("Delete leads")
    st.caption(
        "Permanently removes leads from this campaign, including their activity history — "
        "useful for cleaning up a bad import or duplicates. This can't be undone."
    )
    delete_map = {_lead_option_label(l): l["id"] for l in leads}
    to_delete = st.multiselect("Select leads to delete", list(delete_map.keys()), key="bulk_delete_select")
    if to_delete:
        confirm_key = f"confirm_bulk_delete_{campaign['id']}"
        if st.button(f"Delete {len(to_delete)} selected lead(s)", key="bulk_delete_btn"):
            st.session_state[confirm_key] = True
        if st.session_state.get(confirm_key):
            st.warning(f"This permanently deletes {len(to_delete)} lead(s) and their activity history.")
            if st.button("Yes, delete permanently", type="primary", key="bulk_delete_confirm_btn"):
                db.delete_leads([delete_map[label] for label in to_delete])
                st.session_state[confirm_key] = False
                st.success(f"Deleted {len(to_delete)} lead(s).")
                st.rerun()


# ---------------------------------------------------------------------------
# Page: Settings
# ---------------------------------------------------------------------------

def page_settings(campaign: dict) -> None:
    st.header("Settings")
    st.subheader(f"Sequencing — {campaign['name']}")
    new_settings = _settings_widgets(campaign["settings"], key_prefix=f"edit_{campaign['id']}")
    if st.button("Save settings", type="primary"):
        db.update_campaign_settings(campaign["id"], new_settings)
        st.success("Saved.")
        st.rerun()

    st.divider()
    st.subheader("Notifications")
    if st.button("Send a test notification"):
        ok = notifier.notify("LinkedIn Outreach Tracker", "This is a test notification.")
        if ok:
            st.success("Sent — check your system notifications.")
        else:
            st.warning(
                "Couldn't fire a native notification on this system. "
                "On Windows, try `pip install plyer`."
            )
    st.caption(
        "For alerts even when this dashboard isn't open, run `python notifier.py` on a "
        "schedule (cron / Task Scheduler / launchd). It only reads this app's local "
        "database — it doesn't open a browser or touch LinkedIn."
    )

    st.divider()
    st.subheader("Danger zone")
    confirm_key = f"confirm_delete_{campaign['id']}"
    if st.button("Delete this campaign and all its leads"):
        st.session_state[confirm_key] = True
    if st.session_state.get(confirm_key):
        st.warning("This permanently deletes the campaign and every lead/activity record in it.")
        if st.button("Yes, permanently delete", type="primary"):
            db.delete_campaign(campaign["id"])
            st.session_state.selected_campaign_id = None
            st.session_state[confirm_key] = False
            st.rerun()


# ---------------------------------------------------------------------------
# Page: Help & Safety
# ---------------------------------------------------------------------------

def page_help() -> None:
    st.header("Help & safety notes")
    st.markdown(
        """
This tool is a queue and a memory, not an autopilot. It tracks where every
lead is in your sequence, paces new connection requests, holds the right
message text ready to paste, and stops the moment you tell it someone
replied. It never logs into LinkedIn, never clicks anything there, and
stores no LinkedIn password or session -- every action on LinkedIn's side
is one you take yourself, in your own normal, already-logged-in browser.

**Why it's built this way.** LinkedIn's User Agreement prohibits using
bots, scripts, or scraping tools to automate actions like connecting,
messaging, or engagement -- regardless of the volume or intent -- and
LinkedIn actively detects and enforces against exactly this pattern
(bulk connects, templated messages, like-then-message sequences). Accounts
that trip it can be permanently restricted. Keeping every click a real,
manual action from you is what actually keeps your account safe.

**Rough pacing guidance** (community-observed, not officially published by
LinkedIn, and can change):

- Free / Premium accounts: a rolling ~100 connection requests per week is
  a commonly hit ceiling; 15–20 per day is a reasonable steady pace.
- Sales Navigator: somewhat higher, roughly 150–200/week.
- If your acceptance or reply rate drops, slow down rather than pushing
  volume -- that pattern is itself a signal platforms watch for.
- New or "cold" accounts should start well below these numbers and ramp
  up gradually.

Treat these as a starting point and adjust for your own account -- check
LinkedIn's current guidance and your own results periodically, since these
numbers move over time and aren't something this app (or anyone outside
LinkedIn) can guarantee.

**What this app stores.** Your leads, the messages pulled from your sheet,
and a timestamped log of what you marked done all live in your own
Supabase (Postgres) project -- connected via a local `.env` file that's
never committed or sent anywhere. LinkedIn never sees this database, and
nothing about it is shared with anyone else; it's just cloud storage
instead of a file on this machine, so you can back it up or check it from
elsewhere if you want to.
        """
    )


# ---------------------------------------------------------------------------
# Sidebar + router
# ---------------------------------------------------------------------------

if "selected_campaign_id" not in st.session_state:
    st.session_state.selected_campaign_id = None
if "nav" not in st.session_state:
    st.session_state.nav = "Upload & Map"

campaigns = db.list_campaigns()

st.sidebar.title("\U0001F517 Outreach Tracker")

if campaigns:
    id_list = [c["id"] for c in campaigns]
    label_map = {c["id"]: f"{c['name']} ({c['status']})" for c in campaigns}
    if st.session_state.selected_campaign_id not in id_list:
        st.session_state.selected_campaign_id = id_list[0]
        if st.session_state.nav == "Upload & Map":
            pass  # let them stay on Upload & Map if that's where they are
    chosen = st.sidebar.selectbox(
        "Campaign",
        id_list,
        format_func=lambda i: label_map[i],
        index=id_list.index(st.session_state.selected_campaign_id),
    )
    st.session_state.selected_campaign_id = chosen
else:
    st.sidebar.caption("No campaigns yet — start below.")

st.session_state.nav = st.sidebar.radio("Go to", NAV_OPTIONS, index=NAV_OPTIONS.index(st.session_state.nav))

st.sidebar.divider()
st.sidebar.caption("Every LinkedIn action is performed by you, by hand. This app never logs into LinkedIn.")

active_campaign = db.get_campaign(st.session_state.selected_campaign_id) if st.session_state.selected_campaign_id else None

if st.session_state.nav == "Upload & Map":
    page_upload()
elif st.session_state.nav == "Help & Safety":
    page_help()
elif active_campaign is None:
    st.info("Create a campaign first on the 'Upload & Map' page.")
elif st.session_state.nav == "Dashboard":
    page_dashboard(active_campaign)
elif st.session_state.nav == "All Leads":
    page_all_leads(active_campaign)
elif st.session_state.nav == "Settings":
    page_settings(active_campaign)
