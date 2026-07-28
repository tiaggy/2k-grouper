"""Notion reads for the web dashboard: tracked groups (with their display
label), Telegram accounts (with display name/username), and the Capture Log,
bucketed by (group, worker, day). Self-contained rather than extending
notionconfig.py/notionaccounts.py, so this dashboard doesn't depend on
helpers only present on a different, still-unmerged branch (see the PR
description / HISTORY.md for the note to reconcile this once both land).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import requests

import config
import notion
import notion_http


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": config.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _send(method: str, url: str, **kw) -> requests.Response:
    return notion_http.send(method, url, _headers, timeout=20, **kw)


def _query_all(db_id: str, flt: dict | None = None):
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        if flt:
            body["filter"] = flt
        r = _send("POST", f"https://api.notion.com/v1/databases/{db_id}/query", json=body)
        r.raise_for_status()
        d = r.json()
        yield from d.get("results", [])
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")


def _rel_first(props: dict, name: str) -> str | None:
    rel = (props.get(name, {}) or {}).get("relation") or []
    return rel[0]["id"] if rel else None


def _row_date(props: dict) -> dt.date | None:
    start = ((props.get("Date", {}) or {}).get("date") or {}).get("start")
    if not start:
        return None
    try:
        return dt.date.fromisoformat(start[:10])
    except ValueError:
        return None


def load_accounts() -> dict:
    """{page_id: {'name': str|None, 'username': str|None}}."""
    out: dict = {}
    if not config.NOTION_ACCOUNTS_DB_ID:
        return out
    for pg in _query_all(config.NOTION_ACCOUNTS_DB_ID):
        p = pg.get("properties", {})
        name = "".join(t.get("plain_text", "") for t in (p.get("Name", {}).get("title") or []))
        username = "".join(t.get("plain_text", "") for t in (p.get("Username", {}).get("rich_text") or []))
        out[pg["id"]] = {"name": name or None, "username": username or None}
    return out


def load_groups() -> dict:
    """{page_id: {'label': str, 'chat_id': int|None}} for ENABLED tracked
    groups only (an untracked/removed group's history stays queryable in
    Notion, but drops off the live dashboard)."""
    out: dict = {}
    if not config.NOTION_TRACKED_DB_ID:
        return out
    for pg in _query_all(config.NOTION_TRACKED_DB_ID):
        p = pg.get("properties", {})
        if (p.get("Enabled", {}) or {}).get("checkbox") is False:
            continue
        label = "".join(t.get("plain_text", "") for t in (p.get("Label", {}).get("title") or []))
        idv = (p.get("ID", {}) or {}).get("number")
        out[pg["id"]] = {"label": label or (str(int(idv)) if idv is not None else pg["id"][:8]),
                         "chat_id": int(idv) if idv is not None else None}
    return out


def load_records() -> dict:
    """{group_pid: {account_pid: {date: set(intents)}}}. Rows missing a
    Group, Account, Date, or Intent are skipped (can't be placed)."""
    out: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    for pg in notion.query_capture():
        p = pg.get("properties", {})
        gpid = _rel_first(p, "Group")
        apid = _rel_first(p, "Account")
        d = _row_date(p)
        intent = ((p.get("Intent", {}) or {}).get("select") or {}).get("name")
        if not (gpid and apid and d and intent):
            continue
        out[gpid][apid][d].add(intent)
    return out
