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
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """One connection, opened once and reused. Every caller goes through
    `_lock`, so cross-thread reuse (compute_snapshot() runs inside a fresh
    asyncio.to_thread() worker each cycle, not always the same OS thread) is
    safe — check_same_thread=False just disables sqlite3's own redundant
    enforcement of that, since our lock already serializes every access.
    Opening a connection (and re-running CREATE TABLE) per call was the
    actual bottleneck once multi-year support made this ~200+ calls/refresh."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.execute("""CREATE TABLE IF NOT EXISTS week_cache (
            group_id TEXT NOT NULL,
            week_start TEXT NOT NULL,
            data TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (group_id, week_start)
        )""")
        _conn.commit()
    return _conn


def has(group_id: str, week_start_iso: str) -> bool:
    with _lock:
        row = _get_conn().execute(
            "SELECT 1 FROM week_cache WHERE group_id=? AND week_start=?",
            (group_id, week_start_iso),
        ).fetchone()
        return row is not None


def get(group_id: str, week_start_iso: str) -> dict | None:
    with _lock:
        row = _get_conn().execute(
            "SELECT data FROM week_cache WHERE group_id=? AND week_start=?",
            (group_id, week_start_iso),
        ).fetchone()
        return json.loads(row[0]) if row else None


def put(group_id: str, week_start_iso: str, data: dict) -> None:
    with _lock:
        c = _get_conn()
        c.execute(
            "INSERT OR REPLACE INTO week_cache (group_id, week_start, data, computed_at) VALUES (?,?,?,?)",
            (group_id, week_start_iso, json.dumps(data), dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        c.commit()
