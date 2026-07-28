"""Week Approvals in Notion — one row per (Tracked Group, Week Start) the web
dashboard has ever touched. This is deliberately a tiny control-plane table
(just the boolean), mirroring the Bot Config / Tracked Groups convention
elsewhere in this project: Notion holds the small, authoritative flag; the
dashboard's own local cache (dashboard/cache.py) holds the actual frozen
table data. A wiped dashboard cache with an already-approved week will
recompute fresh from current Notion data on next boot rather than replay the
exact historical snapshot — an accepted tradeoff for not duplicating full
table data into Notion (see RUNBOOK.md).
"""
from __future__ import annotations

import datetime as dt

import requests

import config
import notion_http


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": config.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _send(method: str, url: str, *, attempts: int = 8, **kw) -> requests.Response:
    return notion_http.send(method, url, _headers, attempts=attempts, timeout=20, **kw)


def _query_all(flt: dict | None = None):
    db = config.NOTION_APPROVALS_DB_ID
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        if flt:
            body["filter"] = flt
        r = _send("POST", f"https://api.notion.com/v1/databases/{db}/query", json=body)
        r.raise_for_status()
        d = r.json()
        yield from d.get("results", [])
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")


def load() -> dict:
    """{(group_page_id, week_start_iso): approved_bool} for every row. {} on
    failure or if unconfigured — callers should treat that as 'nothing
    approved yet', not as an error that blocks the dashboard."""
    if not (config.NOTION_TOKEN and config.NOTION_APPROVALS_DB_ID):
        return {}
    out: dict = {}
    try:
        for pg in _query_all():
            p = pg.get("properties", {})
            rel = (p.get("Group", {}) or {}).get("relation") or []
            ws = ((p.get("Week Start", {}) or {}).get("date") or {}).get("start")
            approved = bool((p.get("Approved", {}) or {}).get("checkbox"))
            if rel and ws:
                out[(rel[0]["id"], ws[:10])] = approved
        return out
    except Exception as exc:
        print(f"[approvals] load failed: {exc!r}")
        return {}


def _find(group_page_id: str, week_start_iso: str) -> str | None:
    try:
        r = _send("POST", f"https://api.notion.com/v1/databases/{config.NOTION_APPROVALS_DB_ID}/query",
                  json={"filter": {"and": [
                      {"property": "Group", "relation": {"contains": group_page_id}},
                      {"property": "Week Start", "date": {"equals": week_start_iso}},
                  ]}, "page_size": 1})
        r.raise_for_status()
        res = r.json().get("results")
        return res[0]["id"] if res else None
    except Exception:
        return None


def is_approved(group_page_id: str, week_start_iso: str) -> bool:
    """Authoritative, synchronous check of one (group, week) row's Approved
    flag straight from Notion. Used to guard the dashboard's edit-cell write
    path, where the in-memory snapshot isn't safe to trust: a concurrent
    background refresh can silently replace it with a fresh-but-stale read
    (computed from Notion state as of before a just-written approval landed),
    defeating an approved-week check that only looks at that snapshot. Small,
    fast query against this tiny control-plane DB — not the slow Capture Log
    pull. Fails closed on a Notion/network error (query attempted but didn't
    complete): reports approved=True so the caller blocks the edit rather
    than risking a write it couldn't actually confirm was safe. Unconfigured
    (no approvals DB set up at all) is a different, permanent case where
    nothing can ever be approved — same as load()'s convention — so that
    still reports False."""
    if not (config.NOTION_TOKEN and config.NOTION_APPROVALS_DB_ID):
        return False
    try:
        r = _send("POST", f"https://api.notion.com/v1/databases/{config.NOTION_APPROVALS_DB_ID}/query",
                  json={"filter": {"and": [
                      {"property": "Group", "relation": {"contains": group_page_id}},
                      {"property": "Week Start", "date": {"equals": week_start_iso}},
                  ]}, "page_size": 1})
        r.raise_for_status()
        res = r.json().get("results")
        if not res:
            return False
        return bool((res[0].get("properties", {}).get("Approved", {}) or {}).get("checkbox"))
    except Exception:
        return True


def set_approved(group_page_id: str, week_start: dt.date, approved: bool, label: str) -> bool:
    """Create-or-update the (group, week) row's Approved flag. `label` is the
    human-readable title (e.g. 'iSoftBet — Week of 2026-07-20')."""
    if not (config.NOTION_TOKEN and config.NOTION_APPROVALS_DB_ID):
        return False
    ws_iso = week_start.isoformat()
    pid = _find(group_page_id, ws_iso)
    try:
        if pid:
            r = _send("PATCH", f"https://api.notion.com/v1/pages/{pid}",
                      json={"properties": {"Approved": {"checkbox": approved}}})
            return r.status_code == 200
        props = {
            "Label": {"title": [{"type": "text", "text": {"content": label[:200]}}]},
            "Group": {"relation": [{"id": group_page_id}]},
            "Week Start": {"date": {"start": ws_iso}},
            "Approved": {"checkbox": approved},
        }
        r = _send("POST", "https://api.notion.com/v1/pages",
                  json={"parent": {"database_id": config.NOTION_APPROVALS_DB_ID}, "properties": props})
        return r.status_code == 200
    except Exception as exc:
        print(f"[approvals] set_approved failed: {exc!r}")
        return False
