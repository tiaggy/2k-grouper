"""Write captured events into the Notion 'Capture Log' database.

One row per captured Telegram message. Best-effort: any failure is reported by
the caller-supplied logger and never raised into the bot's poll loop.
"""
from __future__ import annotations

import requests

import config
import notion_http

_API = "https://api.notion.com/v1/pages"


def _headers() -> dict:
    """Headers for Capture Log operations (data-source model, newer API version)."""
    return {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": config.NOTION_DS_VERSION,
        "Content-Type": "application/json",
    }


def _send(method: str, url: str, *, attempts: int = 8, **kw) -> requests.Response:
    return notion_http.send(method, url, _headers, attempts=attempts, timeout=30, **kw)


def healthcheck() -> bool:
    """A single quick probe (no retry) of the Capture Log data source — True if
    Notion is reachable and answering 200. Used to gate processing."""
    if not config.NOTION_ENABLED:
        return True
    try:
        r = requests.post(
            f"https://api.notion.com/v1/data_sources/{config.NOTION_DB_DS_ID}/query",
            headers=_headers(), json={"page_size": 1}, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def query_capture(flt: dict | None = None):
    """Yield every Capture Log page (paginated), via its data source. Retries
    rate limits / 5xx. `flt` is an optional Notion filter object."""
    url = f"https://api.notion.com/v1/data_sources/{config.NOTION_DB_DS_ID}/query"
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        if flt:
            body["filter"] = flt
        r = _send("POST", url, json=body)
        r.raise_for_status()
        d = r.json()
        yield from d.get("results", [])
        if not d.get("has_more"):
            break
        cursor = d.get("next_cursor")


def archive_missing(account_page_id: str | None, date_str: str | None) -> int:
    """Remove any `missing` row for this account on this day (archived). Called
    before writing a real record so a later/back-dated signal supersedes a
    previously-swept absence. Returns how many rows were archived. Best-effort."""
    if not (config.NOTION_ENABLED and account_page_id and date_str):
        return 0
    flt = {"and": [
        {"property": "Account", "relation": {"contains": account_page_id}},
        {"property": "Intent", "select": {"equals": "missing"}},
        {"property": "Date", "date": {"equals": date_str[:10]}},
    ]}
    n = 0
    try:
        for pg in query_capture(flt):
            r = _send("PATCH", f"https://api.notion.com/v1/pages/{pg['id']}", json={"archived": True})
            if r.status_code == 200:
                n += 1
    except Exception:
        pass
    return n


def archive_day(account_page_id: str | None, date_str: str | None) -> int:
    """Archive every Capture Log row for this account on this day, regardless
    of intent. Used by the dashboard's manual edit-cell feature: a correction
    replaces whatever was already recorded that day outright, rather than
    reconciling with it. Returns how many rows were archived. Best-effort."""
    if not (config.NOTION_ENABLED and account_page_id and date_str):
        return 0
    flt = {"and": [
        {"property": "Account", "relation": {"contains": account_page_id}},
        {"property": "Date", "date": {"equals": date_str[:10]}},
    ]}
    n = 0
    try:
        for pg in query_capture(flt):
            r = _send("PATCH", f"https://api.notion.com/v1/pages/{pg['id']}", json={"archived": True})
            if r.status_code == 200:
                n += 1
    except Exception:
        pass
    return n


def enforce_day_unresolved(account_page_id: str | None, date_str: str | None) -> int:
    """A worker+day is 'clean' only as a lone record or a single clock_in+clock_out
    pair. Any other multi-record day is ambiguous -> set every record that day to
    `unresolved`. Returns how many rows were changed. Best-effort."""
    if not (config.NOTION_ENABLED and account_page_id and date_str):
        return 0
    flt = {"and": [
        {"property": "Account", "relation": {"contains": account_page_id}},
        {"property": "Date", "date": {"equals": date_str[:10]}},
    ]}
    pages: list = []
    try:
        for pg in query_capture(flt):
            intent = ((pg["properties"].get("Intent", {}) or {}).get("select") or {}).get("name")
            pages.append((pg["id"], intent))
    except Exception:
        return 0
    intents = sorted(i for _, i in pages)
    if len(pages) <= 1 or (len(pages) == 2 and intents == ["clock_in", "clock_out"]):
        return 0
    n = 0
    for pid, intent in pages:
        if intent == "unresolved":
            continue
        r = _send("PATCH", f"https://api.notion.com/v1/pages/{pid}",
                  json={"properties": {"Intent": {"select": {"name": "unresolved"}}}})
        if r.status_code == 200:
            n += 1
    return n


def _text(value: str | None) -> list:
    """A rich_text / title value array (Notion caps text at 2000 chars per block)."""
    return [{"type": "text", "text": {"content": (value or "")[:2000]}}]


def _date_value(date_str: str | None, time_str: str | None) -> str | None:
    """Combine an action date with an optional clock time into a Notion date
    value — 'YYYY-MM-DDTHH:MM:00' when a time is known, else 'YYYY-MM-DD'."""
    if not date_str:
        return None
    if not time_str or "T" in date_str:  # date already carries a time
        return date_str
    t = time_str.strip()
    hh, mm = (t.split(":", 1) + ["00"])[:2] if ":" in t else (t, "00")
    try:
        return f"{date_str}T{int(hh):02d}:{int(mm):02d}:00"
    except ValueError:
        return date_str


def add_event(event: dict, account_page_id: str | None = None,
              group_page_id: str | None = None) -> tuple[bool, str]:
    """Create one page (row) in the Capture Log from a captured event. Identity is
    the Account relation (→ Telegram Accounts) and the Group relation (→ Tracked
    Groups); the title is the raw message. Returns (ok, detail)."""
    if not config.NOTION_ENABLED:
        return False, "notion disabled (missing token or db id)"

    captured_at = event.get("captured_at")
    # Action date: an AI-extracted date if present, otherwise the message's own date.
    dates = event.get("dates") or []
    action_date = dates[0] if dates else ((captured_at or "")[:10] or None)
    date_value = _date_value(action_date, event.get("time"))
    props: dict = {
        "Text": {"title": _text(event.get("text"))},
        "Timestamp": {"date": {"start": captured_at}},
        "Intent": {"select": {"name": event.get("intent") or "other"}},
        "Source": {"select": {"name": event.get("source") or "none"}},
    }
    if account_page_id:
        props["Account"] = {"relation": [{"id": account_page_id}]}
    if group_page_id:
        props["Group"] = {"relation": [{"id": group_page_id}]}
    if date_value:
        props["Date"] = {"date": {"start": date_value}}
    if event.get("confidence") is not None:
        props["Confidence"] = {"number": round(float(event["confidence"]), 2)}

    body = {"parent": {"type": "data_source_id", "data_source_id": config.NOTION_DB_DS_ID},
            "properties": props}
    try:
        r = _send("POST", _API, json=body)  # honors 429 Retry-After / retries 5xx
        if r.status_code == 200:
            return True, r.json().get("id", "")
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, repr(exc)
