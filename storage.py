"""Append captured events to a JSONL log (shadow mode).

Later this module is where the Notion write / Pending Inbox logic will live.
For now it only records what the bot saw and how it classified it, so we can
measure accuracy before trusting any auto-write.
"""
from __future__ import annotations

import json

import config


def record_event(event: dict) -> None:
    with open(config.EVENTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
