"""Append every captured event to a local JSONL audit log, independent of the
Notion write (notion.py) — so there's always a local record of what the bot saw
and how it classified it, even if a Notion write fails.
"""
from __future__ import annotations

import json

import config


def record_event(event: dict) -> None:
    with open(config.EVENTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
