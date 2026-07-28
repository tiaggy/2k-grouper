# 2K Grouper — Telegram attendance capture bot

A private, owner-scoped Telegram bot that reads `+` / `-` clock-in/out messages
(and free-text sick / vacation / holiday notes) from client work-chat groups,
classifies them, and writes one row per event to a Notion **Capture Log** —
linked to Telegram accounts, tracked groups, and (via your Applicant Tracker)
the people behind them.

- **Rules first:** a lone `+`/`-` line is classified instantly, no AI call.
  `+` = clock in. `-` closes the day if there was a clock-in that day (or an
  open `+` within 16h, for overnight shifts); otherwise it's `unresolved`
  (ambiguous — not enough context to call it a day off).
- **AI for everything else** (optional): free-text notes ("sick today",
  "vacation 12-16.01") go to an OpenAI-compatible model, with a cheap fallback
  model if the primary is down. Casual chat/greetings are filtered locally
  and never sent to a model.
- **A day with more than one record** — unless it's a clean clock-in +
  clock-out pair — is ambiguous, so every record that day is marked
  `unresolved` rather than guessed.
- **Daily missing sweep:** at the start of each weekday, every worker expected
  to report (active recently) but with no record yet is marked `missing`;
  each `missing` row is replaced by their real record the moment they report.
- **Resilient by design:** if Notion or the AI backend goes down, the bot
  pauses and durably spools incoming messages locally instead of dropping or
  misclassifying them, then drains the spool and catches up missed days once
  the dependency recovers. Owners get a DM on pause/recovery.
- **Config lives in Notion**, not `.env`: owners, ignored users, and tracked
  groups are rows in Notion databases, editable without a restart. `.env`
  holds only secrets and toggles.
- **Owner surface:** DM the bot `/dashboard` for uptime, quick links to the
  Notion databases, and buttons to toggle AI-assist / debug logging / restart
  the bot. It posts nothing into the tracked groups.

## Quick start (local)

```bash
py -m pip install -r requirements.txt
copy .env.example .env        # PowerShell: Copy-Item .env.example .env
# fill in .env — see the comments in .env.example for every key
py bot.py
```

First run: message the bot privately as an owner to see `/dashboard`; add it
to a group as an owner to start tracking that group (a Tracked Groups row is
created automatically).

## Running in Docker (recommended for a VPS)

```bash
docker compose up -d --build
docker compose logs -f
```

This starts **two** containers — the bot, and the web dashboard (below). See
**[README.docker.md](README.docker.md)** for the container details (why the bot
publishes no ports but the dashboard does, and the Docker + UFW firewall gotcha)
and **[DEPLOY.md](DEPLOY.md)** for a full VPS deployment walkthrough.

## Web dashboard

A read-only, auto-updating attendance dashboard — one combined calendar table
per team (Tracked Group), workers as rows, grouped by week, most recent first.
Runs as its own container (`dashboard/`, `Dockerfile.dashboard`), polling
`/api/data` in the browser every 15s; no login needed to view.

Each week has an **Approve / Not approved** toggle. A **not-approved** week is
always live — recomputed from the Notion Capture Log on a timer
(`DASHBOARD_REFRESH_SECONDS`, default 60s). Approving a week **freezes** it: the
dashboard stops re-deriving that (team, week) from Notion and keeps serving
whatever it last computed, until someone un-approves it again. The Approved flag
itself lives in Notion (a small **Week Approvals** database — `Group` relation +
`Week Start` date + `Approved` checkbox); the frozen table data lives in the
dashboard's own local SQLite cache (`dashboard_cache.db` on its volume).

Approving/un-approving needs `DASHBOARD_APPROVE_TOKEN` (a shared secret, sent as
an `X-Approve-Token` header — the page prompts for it once and remembers it in
the browser). Leave that env var unset to make the dashboard pure view-only.

```bash
py -m pip install -r requirements-dashboard.txt
py -m uvicorn dashboard.server:app --reload   # http://127.0.0.1:8000
```

## Maintenance tools

- `py missing.py [date [end-date]]` — run the missing-sweep standalone/backfill.
- `py simulate_bot.py` — replay the full message history (`../msgs`,
  `../new_msgs`) through the live classification rules and reconcile the
  Capture Log to match. Resumable, rate-limit-safe; useful for rebuilding after
  a schema change or backfilling an export.

## Project layout

| File | Role |
|---|---|
| `bot.py` | Poll loop, message handling, dashboard, owner commands |
| `classifier.py` | Rule-based grammar + AI fallback (with model failover) |
| `notion.py` | Capture Log reads/writes (new data-source API) |
| `notionconfig.py` | Bot Config / Tracked Groups (legacy API) |
| `notionaccounts.py` | Telegram Accounts registry |
| `notionapprovals.py` | Week Approvals reads/writes (web dashboard) |
| `notion_http.py` | Shared retry/backoff policy for all Notion HTTP calls |
| `attendance.py` | Shared day-code/color/week vocabulary (bot + dashboard) |
| `missing.py` | The missing-worker sweep |
| `tracking.py` | Offline cache of tracked group ids (Notion is authoritative) |
| `storage.py` | Local JSONL audit log of every captured event |
| `config.py` | All environment configuration |
| `dashboard/` | The web dashboard (FastAPI backend + static frontend) |

## Security notes

- `.env` is git-ignored — never commit it. If any token in it was ever shared
  outside this machine, rotate it.
- The bot only acts on Notion-tracked chats and only obeys commands from
  Notion-listed owners (with a single `.env` fallback owner in case Notion is
  unreachable).
- The bot container runs as a non-root user and publishes no inbound ports
  (long-polling is outbound-only). The dashboard container also runs as a
  non-root user; it does publish one port, bound to loopback by default — see
  README.docker.md before exposing it further.
