"""Local persistent cache of computed team-week tables (SQLite, one file on
the dashboard's own volume). Notion (notionapprovals.py) holds the
authoritative Approved flag for a (group, week); this cache holds the actual
computed table data — overwritten every refresh cycle while a week is NOT
approved, left untouched (frozen) the moment it is. See notionapprovals.py's
docstring for the tradeoff this implies if the cache volume is ever wiped.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading

import config

_DB_PATH = config.state_path("dashboard_cache.db")
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS week_cache (
        group_id TEXT NOT NULL,
        week_start TEXT NOT NULL,
        data TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (group_id, week_start)
    )""")
    return c


def has(group_id: str, week_start_iso: str) -> bool:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT 1 FROM week_cache WHERE group_id=? AND week_start=?",
            (group_id, week_start_iso),
        ).fetchone()
        return row is not None


def get(group_id: str, week_start_iso: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT data FROM week_cache WHERE group_id=? AND week_start=?",
            (group_id, week_start_iso),
        ).fetchone()
        return json.loads(row[0]) if row else None


def put(group_id: str, week_start_iso: str, data: dict) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO week_cache (group_id, week_start, data, computed_at) VALUES (?,?,?,?)",
            (group_id, week_start_iso, json.dumps(data), dt.datetime.now(dt.timezone.utc).isoformat()),
        )
