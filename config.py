"""Configuration loaded from environment (.env)."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Emergency anti-lockout owner. Owners/ignored-users/tracked-groups live in the
# Notion Bot Config (source of truth); this is only a fallback if Notion is down.
OWNER_USER_ID: int | None = _int_or_none(os.getenv("OWNER_USER_ID"))

# AI classifier backend: an OpenAI-compatible API. OPENAI_BASE_URL may point at a
# custom / self-hosted endpoint; leave it empty for api.openai.com.
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "").strip()
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
# Fallback model tried on the SAME endpoint if the primary model errors / is
# unavailable (deprecated, overloaded, rate-limited). An economical ChatGPT model
# is a good choice so the bot keeps classifying cheaply while the primary is down.
OPENAI_FALLBACK_MODEL: str = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-5.4-mini").strip()


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# When true, the bot mirrors its console log lines to the owner's Telegram DM.
# Startup default only — toggle live with /debug on|off in the owner's private chat.
DEBUG_TO_OWNER: bool = _bool(os.getenv("DEBUG_DM"))

# AI-assist ("partially-AI") mode: when on, the OpenAI model classifies messages
# the rules can't resolve. Startup default only — toggle live with /ai on|off.
# Needs OPENAI_API_KEY to actually run.
AI_ASSIST: bool = _bool(os.getenv("AI_ASSIST"))

# Notion: write each captured event as a row in NOTION_DB_ID. Writing is enabled
# only when both a token and a database id are present.
NOTION_TOKEN: str = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DB_ID: str = os.getenv("NOTION_DB_ID", "").strip()
NOTION_VERSION: str = os.getenv("NOTION_VERSION", "2022-06-28").strip()
# The Capture Log uses Notion's multi-source model: writes/queries go to its data
# source (with the newer API version), not the database id. Other DBs stay legacy.
NOTION_DB_DS_ID: str = os.getenv("NOTION_DB_DS_ID", "").strip()
NOTION_DS_VERSION: str = os.getenv("NOTION_DS_VERSION", "2025-09-03").strip()
NOTION_ENABLED: bool = bool(NOTION_TOKEN and NOTION_DB_DS_ID)

# Bot Config database (Type / ID / Label / Enabled) — source of truth for tracked
# groups, ignored users, and owner ids. Read on startup + refresh.
NOTION_CONFIG_DB_ID: str = os.getenv("NOTION_CONFIG_DB_ID", "").strip()
# Tracked groups live in their own database (with a Client relation); owners and
# ignored users stay in the Bot Config database above.
NOTION_TRACKED_DB_ID: str = os.getenv("NOTION_TRACKED_DB_ID", "").strip()
# Telegram Accounts registry — one row per account seen posting (User ID / Name /
# Username), with a Person relation to the Applicant Tracker (filled by a human).
NOTION_ACCOUNTS_DB_ID: str = os.getenv("NOTION_ACCOUNTS_DB_ID", "").strip()
# Week Approvals — one row per (Group, Week Start) the web dashboard has ever
# touched; Approved=true freezes that team's week from further updates.
NOTION_APPROVALS_DB_ID: str = os.getenv("NOTION_APPROVALS_DB_ID", "").strip()
CONFIG_REFRESH_SECONDS: int = int(os.getenv("CONFIG_REFRESH_SECONDS", "300"))
# When Notion (or, in AI mode, the AI backend) is unavailable, the bot pauses —
# it stops consuming Telegram updates so messages are held (not dropped or
# misclassified) — and re-checks health every this many seconds.
PAUSE_RETRY_SECONDS: int = int(os.getenv("PAUSE_RETRY_SECONDS", "20"))
HEALTH_TTL_SECONDS: int = int(os.getenv("HEALTH_TTL_SECONDS", "20"))
# Notion outages alert owners immediately; a (more transient) AI outage only
# alerts once it has lasted this long — a brief AI blip stays quiet.
AI_ALERT_AFTER_SECONDS: int = int(os.getenv("AI_ALERT_AFTER_SECONDS", "3600"))

# Daily "missing" sweep: once per weekday, write a `missing` Capture Log row for
# each expected worker who has no record yet that day. With MISSING_SWEEP_HOUR=0 it
# runs at the START of the day — everyone is marked `missing` up front and each row
# is replaced by the real record as the worker reports (handled in the bot's write
# path). Set a later hour (e.g. 20) to instead mark only end-of-day no-shows.
# Runs from inside the bot; standalone via `py missing.py [date [end]]`.
MISSING_SWEEP_ENABLED: bool = _bool(os.getenv("MISSING_SWEEP", "on"))
MISSING_SWEEP_HOUR: int = int(os.getenv("MISSING_SWEEP_HOUR", "0"))  # 0-23 local; 0 = day start
# A worker is "expected" if their account appeared in the Capture Log within this
# many days ending at the swept date (bounds who can be marked missing).
MISSING_ROSTER_LOOKBACK_DAYS: int = int(os.getenv("MISSING_ROSTER_LOOKBACK_DAYS", "30"))
# Only mark people whose Person (Applicant Tracker) is hired. Leave off until the
# main Applicant Tracker is shared with this integration; then set it on.
MISSING_REQUIRE_HIRED: bool = _bool(os.getenv("MISSING_REQUIRE_HIRED", "off"))
MISSING_HIRED_PROP: str = os.getenv("MISSING_HIRED_PROP", "Stage").strip()
MISSING_HIRED_VALUES: str = os.getenv("MISSING_HIRED_VALUES", "Hired").strip()

# Runtime state directory. All state files (progress / spool / caches / events log)
# live here so a container can persist them on a mounted volume. Defaults to the
# code directory for a plain `py bot.py` run; set STATE_DIR=/data under Docker.
STATE_DIR: str = os.getenv("STATE_DIR", os.path.dirname(os.path.abspath(__file__))).strip()
os.makedirs(STATE_DIR, exist_ok=True)


def state_path(name: str) -> str:
    return os.path.join(STATE_DIR, name)


# Where captured/classified events are appended (one JSON object per line).
EVENTS_LOG = state_path("events.jsonl")

# On the owner's Restart button: under a supervisor that relaunches the process
# (Docker `restart:`, systemd, NSSM) the bot should just EXIT and let the
# supervisor restart it — set EXIT_ON_RESTART=1. Standalone, it self-respawns.
EXIT_ON_RESTART: bool = _bool(os.getenv("EXIT_ON_RESTART"))

# --- Web dashboard (dashboard/server.py — runs in its own container) --------
# How often the background loop re-derives NOT-approved weeks from the Capture
# Log. Approved weeks are skipped entirely (frozen in the local cache). Each
# cycle re-pulls the WHOLE Capture Log (paginated), so this is deliberately
# gentle rather than near-instant — attendance data doesn't change fast enough
# to need sub-minute latency, and it keeps steady-state Notion API load low as
# the log grows over months. Lower it if you want snappier updates.
DASHBOARD_REFRESH_SECONDS: int = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "60"))
# Shared secret required (as an X-Approve-Token header) for everything that
# touches real attendance data: viewing (/api/data), approving/un-approving a
# week, and editing a day cell. Leave unset and the dashboard has no data
# access at all — it fails closed, not open.
DASHBOARD_APPROVE_TOKEN: str = os.getenv("DASHBOARD_APPROVE_TOKEN", "").strip()


def require_token() -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    return TELEGRAM_BOT_TOKEN
