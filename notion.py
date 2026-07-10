"""Write captured events into the Notion 'Capture Log' database.

One row per captured Telegram message. Best-effort: any failure is reported by
the caller-supplied logger and never raised into the bot's poll loop.
"""
from __future__ import annotations

import requests

import config

_API = "https://api.notion.com/v1/pages"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": config.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _text(value: str | None) -> list:
    """A rich_text / title value array (Notion caps text at 2000 chars per block)."""
    return [{"type": "text", "text": {"content": (value or "")[:2000]}}]


def _date_value(date_str: str | None, time_str: str | None) -> str | None:
    """Combine an action date with an optional clock time into a Notion date
    value — 'YYYY-MM-DDTHH:MM:00' when a time is known, else 'YYYY-MM-DD'."""
    if not date_str:
        return None
    if not time_str or "T" in date_str:  # date already carries a time
        return date_str
    t = time_str.strip()
    hh, mm = (t.split(":", 1) + ["00"])[:2] if ":" in t else (t, "00")
    try:
        return f"{date_str}T{int(hh):02d}:{int(mm):02d}:00"
    except ValueError:
        return date_str


def add_event(event: dict) -> tuple[bool, str]:
    """Create one page (row) in the Capture Log database from a captured event.
    Returns (ok, detail)."""
    if not config.NOTION_ENABLED:
        return False, "notion disabled (missing token or db id)"

    # Worker is the account's display name; the @username goes in its own field
    # (blank when the account has no username set).
    uname = event.get("username")
    worker = event.get("full_name") or uname or str(event.get("user_id") or "?")
    captured_at = event.get("captured_at")
    # Action date: an AI-extracted date if present, otherwise the message's own
    # date; the clock time (if any) is folded into it.
    dates = event.get("dates") or []
    action_date = dates[0] if dates else ((captured_at or "")[:10] or None)
    date_value = _date_value(action_date, event.get("time"))
    props: dict = {
        "Worker": {"title": _text(worker)},
        "Username": {"rich_text": _text(uname)},
        "Group": {"rich_text": _text(event.get("chat_title"))},
        "Timestamp": {"date": {"start": captured_at}},
        "Intent": {"select": {"name": event.get("intent") or "other"}},
        "Text": {"rich_text": _text(event.get("text"))},
        "Source": {"select": {"name": event.get("source") or "none"}},
    }
    if date_value:
        props["Date"] = {"date": {"start": date_value}}
    # Numbers only when present (Notion rejects null for a number property value).
    if event.get("user_id") is not None:
        props["User ID"] = {"number": event["user_id"]}
    if event.get("confidence") is not None:
        props["Confidence"] = {"number": round(float(event["confidence"]), 2)}

    body = {"parent": {"database_id": config.NOTION_DB_ID}, "properties": props}
    try:
        r = requests.post(_API, headers=_headers(), json=body, timeout=20)
        if r.status_code == 200:
            return True, r.json().get("id", "")
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, repr(exc)
