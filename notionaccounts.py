"""Telegram Accounts registry in Notion.

Records each account (by user id) seen posting in a tracked group — with its
name and username — the first time it's seen, and exposes the account's Notion
page id so the Capture Log can relate records to it. A human links each row to
the matching Applicant Tracker person via the Person relation.
"""
from __future__ import annotations

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


def load() -> dict | None:
    """Map of {user_id: page_id} for every account row, or None on failure."""
    if not (config.NOTION_TOKEN and config.NOTION_ACCOUNTS_DB_ID):
        return {}
    out: dict = {}
    cursor = None
    try:
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = _send("POST", f"https://api.notion.com/v1/databases/{config.NOTION_ACCOUNTS_DB_ID}/query", json=body)
            r.raise_for_status()
            d = r.json()
            for pg in d.get("results", []):
                idv = (pg.get("properties", {}).get("User ID", {}) or {}).get("number")
                if idv is not None:
                    out[int(idv)] = pg["id"]
            if not d.get("has_more"):
                break
            cursor = d.get("next_cursor")
        return out
    except Exception as exc:
        print(f"[accounts] load failed: {exc!r}")
        return None


def _find(user_id: int) -> str | None:
    """Page id of the row with this User ID, if it exists."""
    try:
        r = _send("POST", f"https://api.notion.com/v1/databases/{config.NOTION_ACCOUNTS_DB_ID}/query",
                  json={"filter": {"property": "User ID", "number": {"equals": user_id}}, "page_size": 1})
        r.raise_for_status()
        res = r.json().get("results")
        return res[0]["id"] if res else None
    except Exception:
        return None


def get_or_create(user_id: int, name: str | None, username: str | None) -> str | None:
    """Return the account's Notion page id, creating the row if it's new."""
    if not (config.NOTION_TOKEN and config.NOTION_ACCOUNTS_DB_ID) or user_id is None:
        return None
    pid = _find(user_id)
    if pid:
        return pid
    props = {
        "Name": {"title": [{"type": "text", "text": {"content": (name or username or str(user_id))[:200]}}]},
        "User ID": {"number": user_id},
        "Username": {"rich_text": [{"type": "text", "text": {"content": (username or "")[:200]}}]},
    }
    try:
        r = _send("POST", "https://api.notion.com/v1/pages",
                  json={"parent": {"database_id": config.NOTION_ACCOUNTS_DB_ID}, "properties": props})
        r.raise_for_status()
        return r.json().get("id")
    except Exception as exc:
        print(f"[accounts] create failed: {exc!r}")
        return None
