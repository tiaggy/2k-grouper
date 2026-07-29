"""Attendance web dashboard, one combined calendar table per team (Tracked
Group), grouped by week. A background loop re-derives every NOT-approved
week from the live Notion Capture Log on a timer; an approved week is frozen
(served from the local cache, never recomputed) until un-approved. The static
shell (index.html, dashboard.js/css) has no auth — it carries no real data.
Everything that does — /api/data, approving/un-approving a week, and
manually correcting a day cell (only possible while its week is not
approved) — requires DASHBOARD_APPROVE_TOKEN. If that env var isn't set, the
dashboard has no data access at all (fails closed), not a public fallback.

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
import notion
import notionapprovals
from dashboard import cache, notion_data

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_state_lock = threading.Lock()
_state: dict = {"snapshot": None, "error": None}


def _all_years(records: dict) -> list:
    """Every calendar year with at least one record, anywhere, plus the
    current year always (so the dropdown never comes up empty)."""
    years = {dt.date.today().year}
    for group_records in records.values():
        for by_day in group_records.values():
            years.update(d.year for d in by_day)
    return sorted(years)


def _year_weeks(year: int) -> list:
    """Every Monday-aligned week of `year`, Jan through Dec (the first/last
    week may spill a few days into the neighboring year — standard calendar-
    week convention, e.g. Excel/ISO week numbering)."""
    w = attendance.week_start(dt.date(year, 1, 1))
    end = dt.date(year, 12, 31)
    weeks = []
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
        workers.append({"name": name, "username": acc.get("username"), "account_id": apid, "days": week_codes})
    workers.sort(key=lambda w: w["name"].lower())
    return {"days": day_isos, "workers": workers}


def _year_snapshot(year: int, today: dt.date, groups: dict, accounts: dict,
                   records: dict, approvals: dict) -> dict:
    full_year_weeks = _year_weeks(year)  # already chronological, Jan -> Dec
    teams = []
    for gpid, ginfo in groups.items():
        group_records = records.get(gpid)
        year_dates = [d for by_day in group_records.values() for d in by_day if d.year == year] if group_records else []
        if not year_dates:
            continue  # this team has no data at all in this particular year
        # Trim leading weeks before this team's own first record that year —
        # don't show a run of empty weeks just because some OTHER team (or a
        # later year in general) started earlier.
        team_start_week = attendance.week_start(min(year_dates))
        year_weeks = [w for w in full_year_weeks if w >= team_start_week]
        weeks_out = []
        for week in year_weeks:
            ws_iso = week.isoformat()
            approved = approvals.get((gpid, ws_iso), False)
            if not approved or not cache.has(gpid, ws_iso):
                table = _compute_week_table(group_records, accounts, week)
                cache.put(gpid, ws_iso, table)
            else:
                table = cache.get(gpid, ws_iso)
            weeks_out.append({
                "week_start": ws_iso,
                "week_end": (week + dt.timedelta(days=6)).isoformat(),
                "approved": approved,
                "table": table,
            })
        teams.append({"group_id": gpid, "label": ginfo["label"], "weeks": weeks_out})
    teams.sort(key=lambda t: t["label"].lower())
    current_week = attendance.week_start(today)
    return {
        "year": year,
        "current_week_start": current_week.isoformat() if year == today.year else None,
        "teams": teams,
    }


def compute_snapshot() -> dict:
    groups = notion_data.load_groups()
    accounts = notion_data.load_accounts()
    records = notion_data.load_records()
    approvals = notionapprovals.load()

    today = dt.date.today()
    available_years = _all_years(records)
    years = {str(y): _year_snapshot(y, today, groups, accounts, records, approvals) for y in available_years}

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "available_years": available_years,
        "years": years,
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


def _require_token(x_approve_token: str) -> None:
    """Shared gate for every endpoint that touches real attendance data or
    mutates it — viewing and editing use the same DASHBOARD_APPROVE_TOKEN, so
    there's one secret to hand out, not two. The static shell (index.html,
    dashboard.js/css) stays unauthenticated on purpose: it carries no real
    data, just app code, so serving it doesn't need a token check — only
    /api/data (the actual attendance) and the mutating endpoints do."""
    if not config.DASHBOARD_APPROVE_TOKEN:
        raise HTTPException(status_code=403, detail="dashboard access is not configured (no DASHBOARD_APPROVE_TOKEN set on the server)")
    if x_approve_token != config.DASHBOARD_APPROVE_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Approve-Token")


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/api/data")
def api_data(x_approve_token: str = Header(default="")):
    _require_token(x_approve_token)
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
    _require_token(x_approve_token)

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

    year_key = str(week_start.year)
    with _state_lock:
        snapshot = _state["snapshot"]
    label = group_id[:8]
    if snapshot:
        for team in snapshot["years"].get(year_key, {}).get("teams", []):
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
            for team in snapshot["years"].get(year_key, {}).get("teams", []):
                if team["group_id"] != group_id:
                    continue
                for week in team["weeks"]:
                    if week["week_start"] == week_start_str:
                        week["approved"] = approved
    return {"ok": True}


def _find_worker_display(team: dict, account_id: str) -> tuple[str, str | None]:
    """Look up a worker's name/username from any OTHER week of this same team
    in the current snapshot — needed when a manual edit adds a worker's first
    record in a week where they previously had none, so that week's `workers`
    list doesn't yet contain them."""
    for week in team["weeks"]:
        for w in week["table"]["workers"]:
            if w.get("account_id") == account_id:
                return w["name"], w.get("username")
    return account_id[:8], None


@app.post("/api/edit-day")
async def api_edit_day(request: Request, x_approve_token: str = Header(default="")):
    _require_token(x_approve_token)

    body = await request.json()
    group_id = body.get("group_id")
    account_id = body.get("account_id")
    date_str = body.get("date")
    code = (body.get("code") or "").strip().upper()  # "" means clear (no record)
    if not (group_id and account_id and date_str):
        raise HTTPException(status_code=400, detail="group_id, account_id and date are required")
    if code and code not in attendance.CODE_INTENT:
        raise HTTPException(status_code=400, detail=f"unknown code {code!r}")
    try:
        day = dt.date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    week_start_str = attendance.week_start(day).isoformat()
    year_key = str(day.year)

    # Authoritative check straight from Notion, NOT the in-memory snapshot: a
    # concurrent background refresh can replace that snapshot with a fresh
    # read at any moment, computed from a Notion state captured *before* a
    # just-written approval landed — trusting it here would make this guard
    # racy against the very thing it exists to prevent. (Found live: a manual
    # test hit exactly this window and let an edit through against what the
    # UI had just shown as an approved week.)
    if notionapprovals.is_approved(group_id, week_start_str):
        raise HTTPException(status_code=409, detail="this week is approved — un-approve it before editing")

    notion.archive_day(account_id, date_str)
    if code:
        intent = attendance.CODE_INTENT[code]
        ok, detail = notion.add_event(
            {"captured_at": f"{date_str}T12:00:00", "text": "Manual correction via dashboard",
             "intent": intent, "source": "manual", "confidence": 1.0, "dates": [date_str]},
            account_page_id=account_id, group_page_id=group_id,
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"failed to write to Notion: {detail}")

    # Reflect immediately in the in-memory snapshot rather than waiting for the
    # next refresh cycle (which can take a minute — the full Capture Log pull
    # is slow), mirroring /api/approve's pattern. Purely cosmetic (the next
    # poll or refresh cycle would show the correct state regardless), so
    # unlike the approved-week check above, reading the in-memory snapshot
    # here is fine even though it can occasionally be momentarily stale.
    with _state_lock:
        snapshot = _state["snapshot"]
        team = None
        if snapshot:
            for t in snapshot["years"].get(year_key, {}).get("teams", []):
                if t["group_id"] == group_id:
                    team = t
                    break
        week = None
        if team:
            for w in team["weeks"]:
                if w["week_start"] == week_start_str:
                    week = w
                    break
        if snapshot and team is not None and week is not None:
            worker = next((w for w in week["table"]["workers"] if w.get("account_id") == account_id), None)
            if worker is None and code:
                name, username = _find_worker_display(team, account_id)
                worker = {"name": name, "username": username, "account_id": account_id, "days": {}}
                week["table"]["workers"].append(worker)
                week["table"]["workers"].sort(key=lambda w: w["name"].lower())
            if worker is not None:
                if code:
                    worker["days"][date_str] = code
                else:
                    worker["days"].pop(date_str, None)
    return {"ok": True}
