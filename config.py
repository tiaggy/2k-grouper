"""Configuration loaded from environment (.env)."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


def _id_set(value: str | None) -> set[int]:
    out: set[int] = set()
    for part in (value or "").split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_USER_ID: int | None = _int_or_none(os.getenv("OWNER_USER_ID"))
ALLOWED_CHAT_IDS: set[int] = _id_set(os.getenv("ALLOWED_CHAT_IDS"))
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLASSIFIER_MODEL: str = os.getenv("CLASSIFIER_MODEL", "claude-haiku-4-5").strip()

# AI classifier backend: an OpenAI-compatible API. OPENAI_BASE_URL may point at a
# custom / self-hosted endpoint; leave it empty for api.openai.com.
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "").strip()
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# When true, the bot mirrors its console log lines to the owner's Telegram DM.
# Startup default only — toggle live with /debug on|off in the owner's private chat.
DEBUG_TO_OWNER: bool = _bool(os.getenv("DEBUG_DM"))

# AI-assist ("partially-AI") mode: when on, a light model classifies messages the
# rules can't resolve. Startup default only — toggle live with /ai on|off. Needs
# ANTHROPIC_API_KEY to actually run.
AI_ASSIST: bool = _bool(os.getenv("AI_ASSIST"))

# Notion: write each captured event as a row in NOTION_DB_ID. Writing is enabled
# only when both a token and a database id are present.
NOTION_TOKEN: str = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DB_ID: str = os.getenv("NOTION_DB_ID", "").strip()
NOTION_VERSION: str = os.getenv("NOTION_VERSION", "2022-06-28").strip()
NOTION_ENABLED: bool = bool(NOTION_TOKEN and NOTION_DB_ID)

# In shadow mode the bot only reads, classifies, and logs — it writes nothing to
# Notion and posts nothing back into the groups. Flip to False only once the
# capture pipeline (Notion write + confirmation flow) is built and reviewed.
SHADOW_MODE: bool = True

# Where captured/classified events are appended (one JSON object per line).
EVENTS_LOG = os.path.join(os.path.dirname(__file__), "events.jsonl")


def require_token() -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    return TELEGRAM_BOT_TOKEN
