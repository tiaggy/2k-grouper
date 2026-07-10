"""Dynamic tracked-group enrollment, persisted to tracked_chats.json.

The bot tracks a group when the OWNER adds it, and stops when the bot is
removed. This replaces hand-editing ALLOWED_CHAT_IDS: enrollment becomes
"owner adds the bot -> tracking starts; owner (or anyone) removes it -> stops".

The static config.ALLOWED_CHAT_IDS still works and is unioned in, so an id
pinned in .env is always tracked regardless of this file.
"""
from __future__ import annotations

import json
import os

TRACKED_FILE = os.path.join(os.path.dirname(__file__), "tracked_chats.json")


def load() -> dict[str, dict]:
    if not os.path.exists(TRACKED_FILE):
        return {}
    try:
        with open(TRACKED_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[tracking] could not read {TRACKED_FILE}: {exc!r}")
        return {}


def save(data: dict[str, dict]) -> None:
    tmp = TRACKED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TRACKED_FILE)  # atomic on the same filesystem


def ids(data: dict[str, dict]) -> set[int]:
    """The chat ids currently tracked, as ints."""
    out: set[int] = set()
    for k in data:
        try:
            out.add(int(k))
        except (TypeError, ValueError):
            pass
    return out
