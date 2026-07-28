"""Generate one local Excel workbook per hired worker: a year-by-year
attendance calendar built from the Notion Capture Log, styled after the
original "iSoftBet vacations.xlsx" reference — a merged month-header row, a
day-of-week row, a date row, and one compact, colored status cell per day,
followed by per-status summary counts (live COUNTIF formulas, as in the
original). Unlike the original, this covers every intent the bot actually
records (not just vacation/sick), and carries no vacation-entitlement/
carry-over bookkeeping (this system doesn't track that).

    py export_calendars.py                # hired workers only (see fallback below)
    py export_calendars.py --all           # every worker with any Capture Log record
    py export_calendars.py --out DIR       # output directory (default: calendars/)

"Hired" is determined the same way missing.py's MISSING_REQUIRE_HIRED does:
Telegram Accounts -> Person (Applicant Tracker) -> Stage. If no account has
its Person relation linked yet, there's nothing to check — this prints a
warning and falls back to exporting every worker with at least one record,
rather than silently producing nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import missing
import notion
import notionaccounts

# --- status vocabulary -------------------------------------------------------
# A day showing clock_in and/or clock_out (a normal in/out pair, or either
# alone) collapses to Present. Every other intent maps 1:1. Any day whose
# recorded intents don't match one of these clean shapes (shouldn't happen —
# the bot's own multi-record rule already collapses ambiguous days to a single
# `unresolved` — but historical data predating that rule might) falls back to
# 'U' and is counted as an anomaly for the run summary.
_PRESENT_INTENTS = {"clock_in", "clock_out"}
_INTENT_CODE = {
    "vacation": "V", "sick": "S", "public_holiday": "H",
    "day_off": "O", "unresolved": "U", "missing": "X",
}
_LEGEND = [
    ("P", "Present (clock-in recorded)"),
    ("V", "Vacation"),
    ("S", "Sick"),
    ("H", "Public holiday"),
    ("O", "Day off (explicit)"),
    ("U", "Unresolved — needs review"),
    ("X", "Missing — expected, no record"),
]

# Per-code (fill, font) hex pairs — a light tint of a base hue behind a
# darkened, bold version of the same hue, each pair contrast-checked to clear
# WCAG AA (>=4.5:1) at 8pt bold. See HISTORY.md-style reasoning: these are the
# dataviz-skill categorical/status hues (green/blue/magenta/violet/aqua =
# identity; warning-yellow/critical-red = state), tuned per-hue for legibility
# rather than a single global tint/shade amount (the bright hues — magenta,
# aqua, warning-yellow — need much more darkening to stay readable as text).
_CODE_STYLE = {
    "P": ("D1E9D1", "007600"),
    "V": ("D9E7F8", "2467B8"),
    "S": ("FBE7EF", "9B526E"),
    "H": ("DEDCEF", "4A3AA7"),
    "O": ("D6F1E7", "137954"),
    "U": ("FEF1D6", "91670F"),
    "X": ("F7DCDC", "B73434"),
}
_WEEKEND_FILL = "F2F2F2"  # blank Sat/Sun cells (no record expected)


def _argb(hex6: str) -> str:
    """openpyxl stores a bare 6-hex color with a '00' (transparent) alpha
    prefix, not 'FF' (opaque) — a well-known gotcha that can render as no
    fill / no color at all in real Excel. Always go through this."""
    return f"FF{hex6}"


def _fill(hex6: str) -> PatternFill:
    return PatternFill("solid", fgColor=_argb(hex6))


# --- chrome styling, matched to the iSoftBet vacations.xlsx reference -------
_FONT_HDR = "Trebuchet MS"
_MONTH_FILL = _fill("DCBFB0")
_DOW_FILL = _fill("EEDFD8")
_THIN = Side(style="thin")
_CENTER = Alignment(horizontal="center", vertical="center")
_DAY_BORDER = Border(left=_THIN, right=_THIN, bottom=_THIN)
_BOTTOM_BORDER = Border(bottom=_THIN)

_DOW_LABELS = ["Mo", "Tu", "W", "Th", "F", "Sa", "Su"]


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


def _load_records() -> tuple[dict, dict, int, int]:
    """{account_pid: {date: set(intents)}}, {account_pid: first_date}, plus
    (skipped_no_account_or_date, ) counts for the run summary."""
    by_account: dict = defaultdict(lambda: defaultdict(set))
    first_seen: dict = {}
    skipped = 0
    for pg in notion.query_capture():
        p = pg.get("properties", {})
        apid = _rel_first(p, "Account")
        d = _row_date(p)
        intent = ((p.get("Intent", {}) or {}).get("select") or {}).get("name")
        if not apid or d is None or not intent:
            skipped += 1
            continue
        by_account[apid][d].add(intent)
        if apid not in first_seen or d < first_seen[apid]:
            first_seen[apid] = d
    return by_account, first_seen, skipped


def _day_code(intents: set) -> tuple[str, bool]:
    """(code, is_anomaly)."""
    if intents and intents <= _PRESENT_INTENTS:
        return "P", False
    if len(intents) == 1:
        code = _INTENT_CODE.get(next(iter(intents)))
        if code:
            return code, False
    return "U", True


def _target_accounts(accounts: list[dict], all_workers: bool) -> tuple[list[dict], str]:
    """Which accounts to export, and a message explaining the selection."""
    if all_workers:
        return accounts, "--all: exporting every worker with at least one record"

    linked = [a for a in accounts if a["person_id"]]
    if not linked:
        return accounts, (
            "WARNING: no Telegram Account has its Person relation linked yet, so "
            "hired-status can't be checked — exporting every worker with at least "
            "one record instead. Link Person on the accounts you want filtered, "
            "then re-run without --all."
        )

    hired_ids = missing._hired_person_ids({a["person_id"] for a in linked})
    hired = [a for a in linked if a["person_id"] in hired_ids]
    return hired, f"{len(hired)} of {len(linked)} linked worker(s) are marked Hired"


def _safe_filename(account: dict) -> str:
    base = account.get("name") or account.get("username") or f"user_{account['user_id']}"
    base = re.sub(r"[^\w\- ]+", "", base).strip().replace(" ", "_")
    return (base or f"user_{account['user_id']}") + ".xlsx"


def _build_sheet(ws, year: int, account: dict, day_codes: dict, tracked_since: dt.date) -> None:
    jan1 = dt.date(year, 1, 1)
    dec31 = dt.date(year, 12, 31)
    n_days = (dec31 - jan1).days + 1
    first_day_col = 4  # A/B/C are identity columns; day columns start at D

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 13
    for i in range(n_days):
        ws.column_dimensions[get_column_letter(first_day_col + i)].width = 2.6

    # Identity header (row 3) + the worker's single data row (row 4).
    for col, label, value in ((1, "Name", account.get("name") or ""),
                              (2, "Username", account.get("username") or "")):
        ws.cell(row=3, column=col, value=label).font = Font(name="Arial", size=10)
        ws.cell(row=4, column=col, value=value).font = Font(name="Arial", size=10)
    ws.cell(row=3, column=3, value="Tracked since").font = Font(name="Arial", size=10)
    c = ws.cell(row=4, column=3, value=tracked_since)
    c.font = Font(name="Arial", size=10)
    c.number_format = "YYYY-MM-DD"

    # Month header (row 1, merged per month), day-of-week (row 2), date (row 3).
    d = jan1
    col = first_day_col
    month_start_col = {1: first_day_col}
    while d <= dec31:
        if d.day == 1 and d.month != 1:
            month_start_col[d.month] = col
        dow = ws.cell(row=2, column=col, value=_DOW_LABELS[d.weekday()])
        dow.font = Font(name=_FONT_HDR, size=8, bold=True)
        dow.fill = _DOW_FILL
        dow.alignment = _CENTER
        date_cell = ws.cell(row=3, column=col, value=dt.datetime(d.year, d.month, d.day))
        date_cell.font = Font(name=_FONT_HDR, size=8)
        date_cell.fill = _DOW_FILL
        date_cell.alignment = _CENTER
        date_cell.number_format = "D"
        date_cell.border = _BOTTOM_BORDER

        code = day_codes.get(d)
        day_cell = ws.cell(row=4, column=col)
        day_cell.font = Font(name=_FONT_HDR, size=8, bold=True)
        day_cell.alignment = _CENTER
        day_cell.border = _DAY_BORDER
        if code:
            fill, font_color = _CODE_STYLE[code]
            day_cell.value = code
            day_cell.fill = _fill(fill)
            day_cell.font = Font(name=_FONT_HDR, size=8, bold=True, color=_argb(font_color))
        elif d.weekday() >= 5:
            day_cell.fill = _fill(_WEEKEND_FILL)

        d += dt.timedelta(days=1)
        col += 1

    months = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    for m, start_col in month_start_col.items():
        end_col = (month_start_col.get(m + 1, col) - 1)
        ws.merge_cells(start_row=1, start_column=start_col, end_row=1, end_column=end_col)
        cell = ws.cell(row=1, column=start_col, value=months[m - 1])
        cell.font = Font(name=_FONT_HDR, size=8, bold=True, color=_argb("333333"))
        cell.fill = _MONTH_FILL
        cell.alignment = _CENTER
        cell.border = _BOTTOM_BORDER

    # Summary counts (row 3 header letter, row 4 live COUNTIF), one blank
    # spacer column after the day grid.
    last_day_col = col - 1
    day_range = f"{get_column_letter(first_day_col)}4:{get_column_letter(last_day_col)}4"
    summary_col = last_day_col + 2
    for code, _ in _LEGEND:
        hdr = ws.cell(row=3, column=summary_col, value=code)
        hdr.font = Font(name="Arial", size=10, bold=True)
        hdr.alignment = _CENTER
        val = ws.cell(row=4, column=summary_col, value=f'=COUNTIF({day_range},"{code}")')
        val.font = Font(name="Arial", size=10)
        val.alignment = _CENTER
        summary_col += 1

    # Legend, below the single data row.
    ws.cell(row=6, column=1, value="Legend").font = Font(name="Arial", size=11, bold=True)
    for i, (code, label) in enumerate(_LEGEND):
        r = 7 + i
        fill, font_color = _CODE_STYLE[code]
        badge = ws.cell(row=r, column=1, value=code)
        badge.font = Font(name=_FONT_HDR, size=8, bold=True, color=_argb(font_color))
        badge.fill = _fill(fill)
        badge.alignment = _CENTER
        badge.border = _DAY_BORDER
        ws.cell(row=r, column=2, value=label).font = Font(name="Arial", size=10)

    note_row = 7 + len(_LEGEND) + 1
    ws.cell(row=note_row, column=1,
           value=f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} from the Notion Capture Log."
           ).font = Font(name="Arial", size=8, italic=True, color=_argb("888888"))

    ws.freeze_panes = "D4"
    ws.sheet_view.showGridLines = False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="export every worker with a record, skipping the hired filter")
    ap.add_argument("--out", default="calendars", help="output directory (default: calendars/)")
    args = ap.parse_args()

    accounts = notionaccounts.all_accounts()
    if not accounts:
        raise SystemExit("No Telegram Accounts found (check Notion config/token).")
    by_pid = {a["page_id"]: a for a in accounts}

    print("Loading Capture Log...")
    by_account, first_seen, skipped_rows = _load_records()

    targets, note = _target_accounts(accounts, args.all)
    print(note)

    os.makedirs(args.out, exist_ok=True)
    written = anomalies_total = 0
    skipped_no_records = []
    for account in targets:
        records = by_account.get(account["page_id"])
        if not records:
            skipped_no_records.append(account.get("name") or account.get("username") or account["user_id"])
            continue

        wb = Workbook()
        wb.remove(wb.active)
        years = sorted({d.year for d in records})
        for year in years:
            day_codes = {}
            year_anomalies = 0
            for d, intents in records.items():
                if d.year != year:
                    continue
                code, is_anomaly = _day_code(intents)
                day_codes[d] = code
                year_anomalies += is_anomaly
            anomalies_total += year_anomalies
            ws = wb.create_sheet(title=str(year))
            _build_sheet(ws, year, account, day_codes, first_seen[account["page_id"]])

        path = os.path.join(args.out, _safe_filename(account))
        wb.save(path)
        written += 1
        print(f"  wrote {path} ({len(years)} year sheet(s), {len(records)} day(s) of records)")

    print(f"\nDONE: {written} workbook(s) written to {args.out}/")
    if skipped_no_records:
        print(f"  {len(skipped_no_records)} target worker(s) had no Capture Log records, skipped: "
             f"{', '.join(map(str, skipped_no_records))}")
    if skipped_rows:
        print(f"  {skipped_rows} Capture Log row(s) skipped (missing Account/Date/Intent)")
    if anomalies_total:
        print(f"  {anomalies_total} day(s) had an unexpected mix of intents and were marked 'U' — "
             f"worth a manual look")


if __name__ == "__main__":
    main()
