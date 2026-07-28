"""Read-only attendance web dashboard, one combined calendar table per team
(Tracked Group), grouped by week. A background loop re-derives every
NOT-approved week from the live Notion Capture Log on a timer; an approved
week is frozen (served from the local cache, never recomputed) until
un-approved. Viewing needs no auth; approving/un-approving a week needs
DASHBOARD_APPROVE_TOKEN if one is configured.

Run:  uvicorn dashboard.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import attendance
import config
import notionapprovals
from dashboard import cache, notion_data

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_state_lock = threading.Lock()
_state: dict = {"snapshot": None, "error": None}


def _week_range(records_for_group: dict) -> list:
    """Every Monday from the group's earliest record's week through the
    current week, inclusive."""
    all_dates = [d for by_day in records_for_group.values() for d in by_day]
    if not all_dates:
        return []
    start = attendance.week_start(min(all_dates))
    end = attendance.week_start(dt.date.today())
    weeks, w = [], start
    while w <= end:
        weeks.append(w)
        w += dt.timedelta(days=7)
    return weeks


def _compute_week_table(group_records: dict, accounts: dict, week: dt.date) -> dict:
    """group_records: {account_pid: {date: set(intents)}} for ONE group."""
    days = [week + dt.timedelta(days=i) for i in range(7)]
    day_isos = [d.isoformat() for d in days]
    workers = []
    for apid, by_day in group_records.items():
        week_codes = {}
        for d in days:
            intents = by_day.get(d)
            if intents:
                code, _ = attendance.day_code(intents)
                week_codes[d.isoformat()] = code
        if not week_codes:
            continue  # this worker wasn't active this particular week
        acc = accounts.get(apid, {})
        name = acc.get("name") or acc.get("username") or apid[:8]
        workers.append({"name": name, "username": acc.get("username"), "days": week_codes})
    workers.sort(key=lambda w: w["name"].lower())
    return {"days": day_isos, "workers": workers}


def compute_snapshot() -> dict:
    groups = notion_data.load_groups()
    accounts = notion_data.load_accounts()
    records = notion_data.load_records()
    approvals = notionapprovals.load()

    teams = []
    for gpid, ginfo in groups.items():
        group_records = records.get(gpid)
        if not group_records:
            continue  # nothing recorded for this team yet
        weeks_out = []
        for week in _week_range(group_records):
            ws_iso = week.isoformat()
            approved = approvals.get((gpid, ws_iso), False)
            if not approved or not cache.has(gpid, ws_iso):
                table = _compute_week_table(group_records, accounts, week)
                cache.put(gpid, ws_iso, table)
            else:
                table = cache.get(gpid, ws_iso)
            weeks_out.append({"week_start": ws_iso, "approved": approved, "table": table})
        weeks_out.sort(key=lambda w: w["week_start"], reverse=True)
        teams.append({"group_id": gpid, "label": ginfo["label"], "weeks": weeks_out})
    teams.sort(key=lambda t: t["label"].lower())

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "teams": teams,
        "legend": attendance.LEGEND,
        "colors": {code: {"fill": fill, "font": font} for code, (fill, font) in attendance.CODE_STYLE.items()},
        "weekend_fill": attendance.WEEKEND_FILL,
    }


async def _refresh_loop() -> None:
    while True:
        try:
            snapshot = await asyncio.to_thread(compute_snapshot)
            with _state_lock:
                _state["snapshot"] = snapshot
                _state["error"] = None
        except Exception as exc:
            with _state_lock:
                _state["error"] = repr(exc)
            print(f"[dashboard] refresh failed: {exc!r}")
        await asyncio.sleep(config.DASHBOARD_REFRESH_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()


app = FastAPI(title="2K Attendance Dashboard", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/api/data")
def api_data():
    with _state_lock:
        snapshot, error = _state["snapshot"], _state["error"]
    if snapshot is None:
        return JSONResponse({"error": error or "not ready yet"}, status_code=503)
    payload = dict(snapshot)
    if error:
        payload["stale_error"] = error  # last refresh failed; still serving the prior good snapshot
    return payload


@app.post("/api/approve")
async def api_approve(request: Request, x_approve_token: str = Header(default="")):
    if config.DASHBOARD_APPROVE_TOKEN:
        if x_approve_token != config.DASHBOARD_APPROVE_TOKEN:
            raise HTTPException(status_code=401, detail="invalid or missing X-Approve-Token")
    else:
        raise HTTPException(status_code=403, detail="approving is disabled (no DASHBOARD_APPROVE_TOKEN configured)")

    body = await request.json()
    group_id = body.get("group_id")
    week_start_str = body.get("week_start")
    approved = bool(body.get("approved"))
    if not group_id or not week_start_str:
        raise HTTPException(status_code=400, detail="group_id and week_start are required")
    try:
        week_start = dt.date.fromisoformat(week_start_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="week_start must be YYYY-MM-DD")

    with _state_lock:
        snapshot = _state["snapshot"]
    label = group_id[:8]
    if snapshot:
        for team in snapshot["teams"]:
            if team["group_id"] == group_id:
                label = team["label"]
                break

    ok = notionapprovals.set_approved(group_id, week_start, approved, f"{label} — Week of {week_start_str}")
    if not ok:
        raise HTTPException(status_code=502, detail="failed to write to Notion")

    # Reflect immediately in the in-memory snapshot rather than waiting for the
    # next refresh cycle — the toggle should feel instant.
    with _state_lock:
        snapshot = _state["snapshot"]
        if snapshot:
            for team in snapshot["teams"]:
                if team["group_id"] != group_id:
                    continue
                for week in team["weeks"]:
                    if week["week_start"] == week_start_str:
                        week["approved"] = approved
    return {"ok": True}
