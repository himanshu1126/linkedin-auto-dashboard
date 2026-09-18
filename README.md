# LinkedIn Outreach Tracker

A personal tool for running a multi-step LinkedIn outreach sequence from a
spreadsheet, without automating LinkedIn itself. The app runs on your own
machine; your data lives in your own Supabase project.

## What this is (and isn't)

You upload a spreadsheet of leads, map its columns (which one is the
LinkedIn profile URL, which ones hold your messages), and the app tracks
each lead through this sequence:

1. Send a connection request
2. Once accepted, like their most recent post
3. Send message 1
4. If they don't reply within N days, send message 2 -- up to 5 messages
5. The moment you mark someone as replied, their sequence stops and the
   app fires a desktop notification

**The app never logs into LinkedIn and never performs an action there for
you.** For every step, it shows you who's next and (for messages) the
exact text to send, with an "Open profile" button -- you do the actual
click yourself, in your own already-logged-in browser. That's a deliberate
choice, not a missing feature: LinkedIn's User Agreement prohibits
automating connections, messages, and engagement with bots or scripts, and
its detection for exactly this pattern is strong. Keeping every action a
real click from you is what keeps the account safe. See **Pacing & safety**
below, and the in-app Help & Safety page.

## Setup

### 1. Database connection (Supabase)

Data lives in a Postgres database on Supabase, not on your laptop. The app
reads the connection details from a `.env` file in this folder (already
created for you if Claude set this up) with:

```
SUPABASE_DB_HOST=db.xxxxxxxxxxxx.supabase.co
SUPABASE_DB_PORT=5432
SUPABASE_DB_NAME=postgres
SUPABASE_DB_USER=postgres
SUPABASE_DB_PASSWORD=your-password
```

`.env` is in `.gitignore` and is never uploaded anywhere by this app --
keep it that way if you ever put this project under version control.

If the app can't connect: the direct `db.<ref>.supabase.co` host is
IPv6-only, and most home/office networks are IPv4-only. If you get a
connection timeout (not an auth error), open your Supabase project ->
Project Settings -> Database -> Connection string, switch to the
**Session pooler** option, and copy its host/port/user into `.env`
instead (the pooler username looks like `postgres.<project-ref>`, not
just `postgres`).

Since the password was shared in plaintext at some point, it's worth
rotating it from that same Database settings page once everything's
working, then updating `.env` to match.

### 2. Run the app

**macOS (easiest):** double-click `run.command`. First run installs a
local Python environment and dependencies; every run after that just
starts the app. If macOS says it's from an unidentified developer,
right-click it and choose **Open** once to allow it.

**Manual (any OS):**

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Either way, the app opens in your browser at `http://localhost:8501`.

## Using it

1. **Upload & Map**: upload your `.xlsx`/`.csv`, pick which column is the
   LinkedIn profile URL, and which columns hold Message 1 through Message
   5 (only Message 1 is required -- map fewer than 5 if that's all you
   have). Name/Company columns are optional, just used for display.
   Message text can include `{ColumnName}` placeholders that pull from any
   other column in that row (see `sample_leads.xlsx` for an example).
   Set your pacing (daily connection cap, days between messages) and
   create the campaign.
2. **Dashboard**: your daily queue. Each section is one step of the
   sequence -- open a profile, do the action on LinkedIn, click the
   matching "mark done" button here. A campaign-wide "Someone replied?"
   picker at the top lets you flag a reply the instant you see one,
   whatever stage that lead is at.
3. **All Leads**: the full list, filterable and exportable to CSV.
4. **Settings**: adjust a campaign's pacing after the fact, send a test
   desktop notification, or delete a campaign.

### Getting notified

Marking someone as replied fires a desktop notification immediately. To
also get nudged when the dashboard isn't open (e.g. once a day), run:

```bash
python notifier.py
```

on a schedule -- cron, macOS `launchd`, or just leave a terminal tab open
with `watch -n 3600 python notifier.py`. It only reads your Supabase
database over the same connection as the app; it doesn't open a browser.

## Pacing & safety

LinkedIn doesn't publish official limits, but based on current, widely
corroborated reporting ([PhantomBuster](https://phantombuster.com/blog/social-selling/linkedin-connection-request-limit/),
[Overloop](https://overloop.com/blog/linkedin-limits)) a rolling **~100
connection requests per week** (on a 7-day rolling window, not a fixed
calendar week) is a commonly observed ceiling for free/Premium accounts,
with Sales Navigator somewhat higher (~150-200/week). The app's default
daily cap (15/day) sits near that edge intentionally -- lower it if:

- the account is new or has been mostly inactive,
- your acceptance rate is dropping,
- or you'd rather stay well clear of any ceiling.

These numbers are community-observed, not official, and can change --
worth rechecking occasionally rather than trusting this file indefinitely.
The single most protective thing this tool does isn't the cap, though --
it's that every action is still a real, manually-performed click from a
real logged-in session, which is what LinkedIn's own detection is
actually built to tell apart from a bot.

## Project layout

```
app.py               Streamlit UI (run this)
db.py                 PostgreSQL/Supabase storage
sequence_engine.py    The state machine: what's due, when, and what's next
excel_io.py           Spreadsheet import + {placeholder} message rendering
notifier.py           Desktop notifications (also runnable standalone)
sample_leads.xlsx     Example input sheet
run.command            Double-click launcher (macOS)
.env                   Your Supabase connection details (gitignored, not shipped)
data/                  Created on first run; only holds local notifier state now
```
