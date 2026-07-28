"""Shared attendance vocabulary: how a worker-day's recorded Capture Log
intents collapse into one calendar status code, and the color/legend for each
code. Used by the web dashboard (dashboard/) and, going forward, by
export_calendars.py — kept as a single module so the two never drift apart.

A day showing clock_in and/or clock_out (a normal in/out pair, or either
alone) collapses to Present. Every other intent maps 1:1. Any day whose
recorded intents don't match one of these clean shapes (shouldn't happen —
the bot's own multi-record rule already collapses ambiguous days to a single
`unresolved` — but is possible on historical data predating that rule) falls
back to 'U' and should be counted as an anomaly by the caller.
"""
from __future__ import annotations

import datetime as dt

PRESENT_INTENTS = {"clock_in", "clock_out"}
INTENT_CODE = {
    "vacation": "V", "sick": "S", "public_holiday": "H",
    "day_off": "O", "unresolved": "U", "missing": "X",
}

# Display order + legend label for each code.
LEGEND = [
    ("P", "Present (clock-in recorded)"),
    ("V", "Vacation"),
    ("S", "Sick"),
    ("H", "Public holiday"),
    ("O", "Day off (explicit)"),
    ("U", "Unresolved — needs review"),
    ("X", "Missing — expected, no record"),
]

# Per-code (fill, font) hex pairs — a light tint of a base hue behind a
# darkened, bold version of the same hue, each pair WCAG-AA contrast checked
# (>=4.5:1). Same values used by export_calendars.py's Excel badges, so the
# web dashboard and the Excel exports read as one visual system. Plain 6-hex
# is fine here (CSS, not Excel ARGB — openpyxl's alpha-prefix gotcha doesn't
# apply to a web page).
CODE_STYLE = {
    "P": ("#D1E9D1", "#007600"),
    "V": ("#D9E7F8", "#2467B8"),
    "S": ("#FBE7EF", "#9B526E"),
    "H": ("#DEDCEF", "#4A3AA7"),
    "O": ("#D6F1E7", "#137954"),
    "U": ("#FEF1D6", "#91670F"),
    "X": ("#F7DCDC", "#B73434"),
}
WEEKEND_FILL = "#F2F2F2"


def day_code(intents: set) -> tuple[str, bool]:
    """(code, is_anomaly)."""
    if intents and intents <= PRESENT_INTENTS:
        return "P", False
    if len(intents) == 1:
        code = INTENT_CODE.get(next(iter(intents)))
        if code:
            return code, False
    return "U", True


def week_start(d: dt.date) -> dt.date:
    """The Monday of the ISO week containing `d`."""
    return d - dt.timedelta(days=d.weekday())
