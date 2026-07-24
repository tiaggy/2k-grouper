"""Offline cache of the tracked group ids (a mirror of the Notion Bot Config).

Notion is the source of truth; this cache only exists so a Notion outage at
startup doesn't leave the bot with zero tracked groups.
"""
from __future__ import annotations

import json
import os

import config

CACHE = config.state_path("tracked_cache.json")


def save(ids) -> None:
    tmp = CACHE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(int(i) for i in ids), f)
        os.replace(tmp, CACHE)
    except Exception as exc:
        print(f"[cache] save failed: {exc!r}")


def load() -> set:
    try:
        with open(CACHE, encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except Exception:
        return set()
