"""Post explicit `missing` Capture Log rows for tracked workers who have no
record on a given weekday.

A worker is "expected" on date D if their account was active (appeared in the
Capture Log) within MISSING_ROSTER_LOOKBACK_DAYS ending at D. If such a worker
has *no* row dated D and (optionally) their Person is Hired, we write one row:
  Account -> the account, Group -> their most-recent group, Date -> D,
  Intent -> "missing", Source -> "auto".

Idempotent: the row it writes counts as "a record for D", so re-running the same
date adds nothing. Weekends are skipped. Owners / ignored users are skipped, as is
anyone with tracking paused (Telegram Accounts -> Paused checkbox).

    py missing.py                 # today (local), if a weekday
    py missing.py 2026-07-10      # one date
    py missing.py 2026-07-01 2026-07-11   # an inclusive weekday range (backfill)
"""
from __future__ import annotations

import datetime as dt
import sys
import time

import config
import notion
import notion_http
import notionaccounts
import notionconfig

_HIRED_VALUES = {v.strip().lower() for v in config.MISSING_HIRED_VALUES.split(",") if v.strip()}
_PACE = 0.34


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": config.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _send(method: str, url: str, **kw):
    return notion_http.send(method, url, _headers, timeout=30, **kw)


def _query_all(db: str, flt: dict | None = None):
    cur = None
    while True:
        body = {"page_size": 100}
        if cur:
            body["start_cursor"] = cur
        if flt:
            body["filter"] = flt
        r = _send("POST", f"https://api.notion.com/v1/databases/{db}/query", json=body)
        r.raise_for_status()
        d = r.json()
        yield from d.get("results", [])
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")


def _rel_first(props: dict, name: str):
    rel = (props.get(name, {}) or {}).get("relation") or []
    return rel[0]["id"] if rel else None


def _row_date(props: dict) -> dt.date | None:
    """The action Date of a Capture Log row (falls back to Timestamp)."""
    for key in ("Date", "Timestamp"):
        start = ((props.get(key, {}) or {}).get("date") or {}).get("start")
        if start:
            try:
                return dt.date.fromisoformat(start[:10])
            except ValueError:
                pass
    return None


def _accounts_map() -> dict:
    """account_page_id -> {'user_id': int|None, 'person': page_id|None}."""
    out: dict = {}
    for pg in _query_all(config.NOTION_ACCOUNTS_DB_ID):
        p = pg.get("properties", {})
        out[pg["id"]] = {
            "user_id": (p.get("User ID", {}) or {}).get("number"),
            "person": _rel_first(p, "Person"),
        }
    return out


def _hired_person_ids(person_ids: set) -> set:
    """Subset of person pages whose Stage is a hired value. Best-effort: a page we
    can't read (not shared with the integration) is treated as not-hired."""
    hired: set = set()
    prop = config.MISSING_HIRED_PROP
    for pid in person_ids:
        try:
            r = _send("GET", f"https://api.notion.com/v1/pages/{pid}")
            if r.status_code != 200:
                continue
            sp = (r.json().get("properties", {}) or {}).get(prop, {}) or {}
            name = ((sp.get("status") or sp.get("select") or {}) or {}).get("name")
            if name and name.strip().lower() in _HIRED_VALUES:
                hired.add(pid)
        except Exception:
            continue
    return hired


def _roster(day: dt.date, lookback_days: int) -> dict:
    """account_page_id -> most-recent group_page_id, for accounts active in the
    [day - lookback, day] window (their group is taken as of `day`)."""
    cutoff = day - dt.timedelta(days=lookback_days)
    best: dict = {}   # apid -> (date, group_pid)
    for pg in notion.query_capture():
        p = pg.get("properties", {})
        if ((p.get("Intent", {}) or {}).get("select") or {}).get("name") == "missing":
            continue  # expectation is built from real signals, not prior missing rows
        apid = _rel_first(p, "Account")
        gpid = _rel_first(p, "Group")
        rdate = _row_date(p)
        if not apid or not gpid or rdate is None or rdate > day:
            continue
        cur = best.get(apid)
        if cur is None or rdate >= cur[0]:
            best[apid] = (rdate, gpid)
    return {a: g for a, (d, g) in best.items() if d >= cutoff}


def _accounted_on(day: dt.date) -> set:
    """Account pages that already have any Capture Log row dated `day`."""
    nxt = day + dt.timedelta(days=1)
    flt = {"and": [
        {"property": "Date", "date": {"on_or_after": day.isoformat()}},
        {"property": "Date", "date": {"before": nxt.isoformat()}},
    ]}
    out: set = set()
    for pg in notion.query_capture(flt):
        apid = _rel_first(pg.get("properties", {}), "Account")
        if apid:
            out.add(apid)
    return out


def sweep(day: dt.date, log=print) -> dict:
    """Write `missing` rows for the given date. Returns a small stats dict."""
    stats = {"date": day.isoformat(), "written": 0, "accounted": 0, "skipped": 0, "failed": 0}
    if not config.NOTION_ENABLED:
        return stats
    if day.weekday() >= 5:          # Sat/Sun — no attendance expected
        stats["weekend"] = True
        return stats

    cfg = notionconfig.load() or {}
    skip_uids = (cfg.get("ignored") or set()) | (cfg.get("owners") or set())
    skip_uids |= notionaccounts.load_paused() or set()  # tracking paused -> no missing rows either

    roster = _roster(day, config.MISSING_ROSTER_LOOKBACK_DAYS)
    accounts = _accounts_map()
    accounted = _accounted_on(day)

    hired_ids = None
    if config.MISSING_REQUIRE_HIRED:
        persons = {accounts.get(a, {}).get("person") for a in roster}
        hired_ids = _hired_person_ids({p for p in persons if p})

    last = 0.0
    for apid, gpid in roster.items():
        info = accounts.get(apid, {})
        if info.get("user_id") in skip_uids:
            stats["skipped"] += 1
            continue
        if hired_ids is not None and info.get("person") not in hired_ids:
            stats["skipped"] += 1
            continue
        if apid in accounted:
            stats["accounted"] += 1
            continue
        event = {
            "captured_at": f"{day.isoformat()}T23:59:00",
            "text": "(missing)", "intent": "missing",
            "confidence": 1.0, "source": "auto", "dates": [day.isoformat()],
        }
        gap = time.monotonic() - last
        if gap < _PACE:
            time.sleep(_PACE - gap)
        ok, detail = notion.add_event(event, account_page_id=apid, group_page_id=gpid)
        last = time.monotonic()
        if ok:
            stats["written"] += 1
        else:
            stats["failed"] += 1
            log(f"[missing] write failed for {apid}: {detail[:80]}")
    return stats


def _dates(argv: list) -> list:
    if not argv:
        return [dt.date.today()]
    start = dt.date.fromisoformat(argv[0])
    end = dt.date.fromisoformat(argv[1]) if len(argv) > 1 else start
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def main() -> None:
    for day in _dates(sys.argv[1:]):
        s = sweep(day)
        print(f"{s['date']}: wrote {s['written']} missing, "
              f"{s['accounted']} accounted, {s['skipped']} skipped, {s['failed']} failed"
              + (" (weekend)" if s.get("weekend") else ""))


if __name__ == "__main__":
    main()
