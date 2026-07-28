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
# The reverse mapping, for the dashboard's manual edit-cell feature: a code
# picked from the choice list needs one concrete intent to write back to
# Notion. P (collapsed clock_in+clock_out) writes as a lone clock_in, which
# day_code() already renders as 'P' on its own.
CODE_INTENT = {"P": "clock_in", **{code: intent for intent, code in INTENT_CODE.items()}}

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

# Per-code (fill, font) hex pairs — sampled directly from this project's own
# Notion "Intent" select property (dark-mode tag pill colors), so the
# dashboard's badges read as the same colors already used in Notion itself:
# solid, muted fill + white text (Notion's own dark-theme tag style), not a
# light-tint-behind-dark-text badge like an earlier iteration used. P (this
# tool's collapsed clock_in+clock_out) uses Notion's clock_in green. All 7
# pairs clear white-text contrast >=5:1.
CODE_STYLE = {
    "P": ("#3F6F54", "#FFFFFF"),  # clock_in green
    "V": ("#8E5835", "#FFFFFF"),  # vacation
    "S": ("#3B6591", "#FFFFFF"),  # sick
    "H": ("#896A2C", "#FFFFFF"),  # public_holiday
    "O": ("#6E5482", "#FFFFFF"),  # day_off
    "U": ("#824E67", "#FFFFFF"),  # unresolved
    "X": ("#755B48", "#FFFFFF"),  # missing
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
