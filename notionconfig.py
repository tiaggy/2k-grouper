"""Read bot config from Notion.

Two source-of-truth databases:
  * Bot Config      — Owner / Ignored User rows (Type / ID / Enabled)
  * Tracked Groups  — one row per group (Label / ID / Enabled / Client relation)

The bot loads this on startup and refreshes on a schedule, so edits in Notion
take effect without a restart. `Enabled` unchecked disables a row.
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


def _query_all(db_id: str):
    """Yield every page in a database (paginated). Retries rate limits / 5xx."""
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = _send("POST", f"https://api.notion.com/v1/databases/{db_id}/query", json=body)
        r.raise_for_status()
        d = r.json()
        for pg in d.get("results", []):
            yield pg
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")


def _enabled(props: dict) -> bool:
    return (props.get("Enabled", {}) or {}).get("checkbox") is not False


def _number(props: dict, name: str = "ID"):
    return (props.get(name, {}) or {}).get("number")


def load() -> dict | None:
    """Return {'tracked': set[int], 'ignored': set[int], 'owners': set[int]},
    or None on failure (caller keeps the previous config)."""
    if not (config.NOTION_TOKEN and config.NOTION_CONFIG_DB_ID):
        return None
    out = {"tracked": set(), "ignored": set(), "owners": set()}
    try:
        # Owners / ignored users from the Bot Config database.
        for pg in _query_all(config.NOTION_CONFIG_DB_ID):
            p = pg.get("properties", {})
            if not _enabled(p):
                continue
            typ = ((p.get("Type", {}) or {}).get("select") or {}).get("name")
            idv = _number(p)
            if idv is None:
                continue
            if typ == "Owner":
                out["owners"].add(int(idv))
            elif typ == "Ignored User":
                out["ignored"].add(int(idv))
            elif typ == "Tracked Group" and not config.NOTION_TRACKED_DB_ID:
                out["tracked"].add(int(idv))  # backward-compat if no separate DB

        # Tracked groups from their own database.
        if config.NOTION_TRACKED_DB_ID:
            for pg in _query_all(config.NOTION_TRACKED_DB_ID):
                p = pg.get("properties", {})
                if not _enabled(p):
                    continue
                idv = _number(p)
                if idv is not None and int(idv) < 0:  # group ids are negative
                    out["tracked"].add(int(idv))
        return out
    except Exception as exc:
        print(f"[notionconfig] load failed: {exc!r}")
        return None


def tracked_pages() -> dict | None:
    """Map of {chat_id: page_id} for tracked groups (so records can relate to the
    group). None on failure."""
    db = config.NOTION_TRACKED_DB_ID
    if not (config.NOTION_TOKEN and db):
        return {}
    out: dict = {}
    try:
        for pg in _query_all(db):
            p = pg.get("properties", {})
            if not _enabled(p):
                continue
            idv = _number(p)
            if idv is not None and int(idv) < 0:
                out[int(idv)] = pg["id"]
        return out
    except Exception as exc:
        print(f"[notionconfig] tracked_pages failed: {exc!r}")
        return None


def last_msg_ids() -> dict:
    """{chat_id: last processed Telegram message_id} from Tracked Groups (so the
    bot can resume without reprocessing). {} on failure or if unset."""
    db = config.NOTION_TRACKED_DB_ID
    if not (config.NOTION_TOKEN and db):
        return {}
    out: dict = {}
    try:
        for pg in _query_all(db):
            p = pg.get("properties", {})
            idv = _number(p)
            last = _number(p, "Last Msg ID")
            if idv is not None and int(idv) < 0 and last is not None:
                out[int(idv)] = int(last)
        return out
    except Exception as exc:
        print(f"[notionconfig] last_msg_ids failed: {exc!r}")
        return {}


def set_last_msg(page_id: str, msg_id: int) -> bool:
    """Persist the last processed message id onto a Tracked Groups row."""
    if not (config.NOTION_TOKEN and page_id):
        return False
    try:
        r = _send("PATCH", f"https://api.notion.com/v1/pages/{page_id}",
                  json={"properties": {"Last Msg ID": {"number": int(msg_id)}}})
        return r.status_code == 200
    except Exception as exc:
        print(f"[notionconfig] set_last_msg failed: {exc!r}")
        return False


def disable_tracked_group(chat_id: int) -> bool:
    """Untrack a group by unchecking `Enabled` on its Tracked Groups row (kept, not
    deleted, so its Client relation and the Capture Log history links survive).
    Returns True if a row was found and updated. Best-effort."""
    db = config.NOTION_TRACKED_DB_ID or config.NOTION_CONFIG_DB_ID
    if not (config.NOTION_TOKEN and db):
        return False
    try:
        r = _send("POST", f"https://api.notion.com/v1/databases/{db}/query",
                  json={"filter": {"property": "ID", "number": {"equals": chat_id}}, "page_size": 5})
        r.raise_for_status()
        updated = False
        for pg in r.json().get("results", []):
            pr = _send("PATCH", f"https://api.notion.com/v1/pages/{pg['id']}",
                       json={"properties": {"Enabled": {"checkbox": False}}})
            updated = updated or pr.status_code == 200
        return updated
    except Exception as exc:
        print(f"[notionconfig] disable_tracked_group failed: {exc!r}")
        return False


def add_tracked_group(chat_id: int, title: str | None) -> None:
    """Persist a newly-enrolled group as a row in the Tracked Groups DB (Client
    relation left blank for you to fill in). If a row already exists, re-enable it
    (a previously-removed group re-added) instead of creating a duplicate.
    Best-effort."""
    db = config.NOTION_TRACKED_DB_ID or config.NOTION_CONFIG_DB_ID
    if not (config.NOTION_TOKEN and db):
        return
    try:
        r = _send("POST", f"https://api.notion.com/v1/databases/{db}/query",
                  json={"filter": {"property": "ID", "number": {"equals": chat_id}}, "page_size": 5})
        r.raise_for_status()
        rows = r.json().get("results", [])
    except Exception:
        rows = []
    if rows:  # already present — make sure it's enabled again
        for pg in rows:
            if (pg.get("properties", {}).get("Enabled", {}) or {}).get("checkbox") is False:
                try:
                    _send("PATCH", f"https://api.notion.com/v1/pages/{pg['id']}",
                         json={"properties": {"Enabled": {"checkbox": True}}})
                except Exception as exc:
                    print(f"[notionconfig] re-enable failed: {exc!r}")
        return
    props = {
        "Label": {"title": [{"type": "text", "text": {"content": (title or str(chat_id))[:200]}}]},
        "ID": {"number": chat_id},
        "Enabled": {"checkbox": True},
    }
    if not config.NOTION_TRACKED_DB_ID:  # writing into the combined Bot Config
        props["Type"] = {"select": {"name": "Tracked Group"}}
    try:
        _send("POST", "https://api.notion.com/v1/pages",
             json={"parent": {"database_id": db}, "properties": props})
    except Exception as exc:
        print(f"[notionconfig] add_tracked_group failed: {exc!r}")
