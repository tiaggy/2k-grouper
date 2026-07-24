"""Full day-by-day bot simulation / reconciler.

Replays the entire message history exactly as a live bot + daily missing-sweep
would have produced it, then reconciles the Capture Log to match (writing only
what's missing, archiving only what's stale — see `_reconcile`):

  * all group exports (../msgs, ../new_msgs) are merged into ONE global timeline,
    deduped by (group, message_id), and sorted by send time
  * each message is run through the same classifier the bot uses (rules first,
    AI for the rest), with casual->ignore, ignored-user skip, per-day expansion
    of ranges, and weekend skip
  * at the end of each weekday, a missing-sweep writes a `missing` row for every
    expected worker (a real signal within the lookback window) with no row that
    day. `missing` rows never feed the roster, so absences don't self-perpetuate
  * a real record supersedes a same-day `missing`; a multi-record day that isn't
    a clean clock_in+clock_out pair is flipped entirely to `unresolved`

Order-dependent work (the stateful +/- rules) runs sequentially in memory; AI
classification is parallelized. The final reconcile is single-threaded and paced
to stay well under Notion's rate limit — safe to re-run (resumable).

Run once:  py simulate_bot.py
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import classifier
import notion
import notionaccounts
import notionconfig

classifier._ai_allowed = lambda key=None: True

MSG_DIRS = [os.path.join(os.path.dirname(__file__), "..", "msgs"),
            os.path.join(os.path.dirname(__file__), "..", "new_msgs")]
AI = bool(config.OPENAI_API_KEY)
LOOKBACK = config.MISSING_ROSTER_LOOKBACK_DAYS
AI_WORKERS = 8


# --- load / helpers --------------------------------------------------------

def _flat(t) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in t)
    return ""


def _group_chat_id(d: dict):
    gid = d.get("id")
    if gid is None:
        return None
    return int(f"-100{gid}") if d.get("type") == "private_supergroup" else -int(gid)


def _is_weekend(date_str: str) -> bool:
    try:
        return dt.date.fromisoformat(date_str[:10]).weekday() >= 5
    except Exception:
        return False


def _load_timeline() -> list:
    """Every message from all export dirs, merged, deduped by (group, message_id)
    — so overlapping week-dumps don't double-count — and sorted by send time."""
    events, seen = [], set()
    files = [f for d in MSG_DIRS for f in sorted(glob.glob(os.path.join(d, "*.json")))]
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        cid = _group_chat_id(d)
        for m in d.get("messages", []):
            if m.get("type") != "message":
                continue
            key = (cid, m.get("id"))
            if m.get("id") is not None and key in seen:
                continue
            seen.add(key)
            text = _flat(m.get("text", ""))
            if not text.strip():
                continue
            digits = "".join(c for c in str(m.get("from_id") or "") if c.isdigit())
            if not digits:
                continue
            try:
                ts = dt.datetime.fromisoformat(m.get("date"))
            except Exception:
                continue
            events.append({"ts": ts, "date": m.get("date"), "day": ts.date(),
                           "uid": int(digits), "name": m.get("from"),
                           "text": text, "cid": cid})
    events.sort(key=lambda e: (e["ts"], e["cid"]))
    return events


# --- phases ----------------------------------------------------------------

def _classify_all(events: list) -> list:
    """signals per message. Rules/casual resolved sequentially (stateful, instant);
    the LLM-bound remainder classified in parallel (stateless)."""
    classifier.reset_state()
    results: list = [None] * len(events)
    ai_idx: list = []
    for i, e in enumerate(events):
        ruled = classifier.parse_rules(e["text"], key=e["uid"], ts=e["ts"])
        if ruled is not None:
            results[i] = ruled
        elif classifier.is_casual(e["text"]):
            results[i] = [classifier.Signal(classifier.IGNORE, 0.95, "rule", e["text"])]
        elif AI:
            ai_idx.append(i)
        else:
            results[i] = [classifier.Signal("other", 0.0, "none", e["text"])]
    print(f"classify: {len(events) - len(ai_idx)} by rules, {len(ai_idx)} via AI "
          f"({AI_WORKERS} workers)...", flush=True)
    if ai_idx:
        def do(i):
            return i, [classifier.classify_llm(events[i]["text"], ts=events[i]["ts"])]
        done = 0
        with ThreadPoolExecutor(max_workers=AI_WORKERS) as ex:
            for fut in as_completed(ex.submit(do, i) for i in ai_idx):
                i, sig = fut.result()
                results[i] = sig
                done += 1
                if done % 100 == 0:
                    print(f"  ...AI {done}/{len(ai_idx)}", flush=True)
    return results


def main() -> None:
    if not config.NOTION_ENABLED:
        raise SystemExit("Notion not configured")
    cfg = notionconfig.load() or {}
    ignored = cfg.get("ignored") or set()
    skip_uids = ignored | (cfg.get("owners") or set())

    gpages = notionconfig.tracked_pages() or {}
    events = _load_timeline()
    if not events:
        print("no messages")
        return
    print(f"AI:{AI} | {len(events)} messages | {events[0]['day']}..{events[-1]['day']} "
          f"| lookback {LOOKBACK}d", flush=True)

    # Resolve every unique account once (cache).
    acct: dict = {}
    for e in events:
        acct.setdefault(e["uid"], e["name"])
    skip_pages: set = set()
    for uid, name in list(acct.items()):
        if uid in ignored:
            continue
        pid = notionaccounts.get_or_create(uid, name, None)
        acct[uid] = pid
        if uid in skip_uids:
            skip_pages.add(pid)
    print(f"accounts resolved: {len([v for k,v in acct.items() if k not in ignored])}", flush=True)

    signals = _classify_all(events)

    # Build the full row list in memory, day by day (real rows + end-of-day missing).
    by_day: dict = {}
    for e in events:
        by_day.setdefault(e["day"], []).append(e)
    start, end = events[0]["day"], events[-1]["day"]

    rows: list = []                 # (account_pid, group_pid, event_dict)
    real_sigs: list = []            # (apid, gpid, date) — real signals only
    accounted: set = set()          # (apid, date)
    tally: Counter = Counter()
    ignored_ct = 0
    idx_of = {id(e): i for i, e in enumerate(events)}   # O(1) signal lookup
    day = start
    while day <= end:
        day_str = day.isoformat()
        for e in by_day.get(day, []):
            uid = e["uid"]
            if uid in ignored:
                continue
            apid = acct.get(uid)
            gpid = gpages.get(e["cid"])
            for s in signals[idx_of[id(e)]]:
                if s.intent == classifier.IGNORE:
                    ignored_ct += 1
                    continue
                for d in classifier.expand_dates(s, day_str):
                    if _is_weekend(d):
                        continue
                    rows.append((apid, gpid, {
                        "captured_at": e["date"], "text": s.text or e["text"],
                        "intent": s.intent, "confidence": s.confidence,
                        "source": s.source, "dates": [d] if d else []}))
                    tally[s.intent] += 1
                    rd = dt.date.fromisoformat(d) if d else day
                    real_sigs.append((apid, gpid, rd))
                    accounted.add((apid, rd))
        if day.weekday() < 5:
            cutoff = day - dt.timedelta(days=LOOKBACK)
            best: dict = {}
            for apid, gpid, rd in real_sigs:
                if rd > day or rd < cutoff or not gpid:
                    continue
                c = best.get(apid)
                if c is None or rd >= c[0]:
                    best[apid] = (rd, gpid)
            for apid, (rd, gpid) in best.items():
                if apid in skip_pages or (apid, day) in accounted:
                    continue
                rows.append((apid, gpid, {
                    "captured_at": f"{day_str}T23:59:00", "text": "(missing)",
                    "intent": "missing", "confidence": 1.0, "source": "auto",
                    "dates": [day_str]}))
                tally["missing"] += 1
                accounted.add((apid, day))
        day += dt.timedelta(days=1)

    # A real record supersedes a `missing` on the same (account, day) — e.g. a
    # back-dated clock-in that arrived after that day's sweep. Drop those missings.
    real_days = {(apid, ev["dates"][0]) for apid, gpid, ev in rows
                 if ev["intent"] != "missing" and ev.get("dates")}
    before = len(rows)
    rows = [r for r in rows
            if not (r[2]["intent"] == "missing" and (r[0], r[2]["dates"][0]) in real_days)]
    superseded = before - len(rows)
    tally["missing"] -= superseded

    # A day with more than one record is only "clean" as a single clock_in+clock_out
    # pair; any other multi-record day (two clock_ins, clock_in + vacation, a 3rd
    # record, ...) is ambiguous -> mark every record that day `unresolved`.
    groups: dict = defaultdict(list)
    for idx, (apid, gpid, ev) in enumerate(rows):
        if ev["intent"] == "missing":
            continue
        d = (ev["dates"][0] if ev.get("dates") else (ev.get("captured_at") or "")[:10])[:10]
        groups[(apid, d)].append(idx)
    flipped = 0
    for idxs in groups.values():
        intents = sorted(rows[i][2]["intent"] for i in idxs)
        if len(idxs) == 1 or (len(idxs) == 2 and intents == ["clock_in", "clock_out"]):
            continue
        for i in idxs:
            if rows[i][2]["intent"] != "unresolved":
                tally[rows[i][2]["intent"]] -= 1
                tally["unresolved"] += 1
                rows[i][2]["intent"] = "unresolved"
                flipped += 1

    reals = sum(v for k, v in tally.items() if k != "missing")
    print(f"target: {len(rows)} rows ({reals} real + {tally['missing']} missing), "
          f"{ignored_ct} ignored, {superseded} missing superseded, "
          f"{flipped} flipped to unresolved (multi-record days)", flush=True)

    _reconcile(rows)


def _row_key(apid, ev) -> tuple:
    """Stable identity for a row: account, action-date, intent, title (as stored)."""
    d = (ev["dates"][0] if ev.get("dates") else (ev.get("captured_at") or "")[:10])[:10]
    return (apid, d, ev["intent"], (ev.get("text") or "")[:2000])


def _cap_key(pg) -> tuple:
    p = pg.get("properties", {})
    apid = _rel_first_id(p, "Account")
    d = ((p.get("Date", {}) or {}).get("date") or {}).get("start", "")[:10]
    intent = ((p.get("Intent", {}) or {}).get("select") or {}).get("name")
    title = "".join(t.get("plain_text", "") for t in (p.get("Text", {}).get("title") or []))
    return (apid, d, intent, title[:2000])


def _rel_first_id(props, name):
    rel = (props.get(name, {}) or {}).get("relation") or []
    return rel[0]["id"] if rel else None


def _reconcile(rows: list) -> None:
    """Converge the Capture Log to `rows` with the FEWEST requests: write only rows
    that aren't already present, archive any leftovers. Resumable and gentle on the
    rate limit — reads current state, writes the delta single-threaded with pacing."""
    target = Counter(_row_key(a, ev) for a, g, ev in rows)
    tmpl: dict = {}                       # key -> (apid, gpid, event) to write
    for a, g, ev in rows:
        tmpl.setdefault(_row_key(a, ev), (a, g, ev))

    current: dict = defaultdict(list)     # key -> [page_id, ...] already in Notion
    for pg in notion.query_capture():
        current[_cap_key(pg)].append(pg["id"])

    to_write, to_archive = [], []
    for key, need in target.items():
        have = len(current.get(key, []))
        if have < need:
            to_write += [tmpl[key]] * (need - have)
    for key, pages in current.items():
        keep = target.get(key, 0)
        to_archive += pages[keep:]        # duplicates / rows not in the target

    print(f"reconcile: {len(to_write)} to write, {len(to_archive)} to archive "
          f"(already correct: {sum(min(len(current.get(k,[])), n) for k,n in target.items())})",
          flush=True)

    wrote = wfail = 0
    for i, (apid, gpid, ev) in enumerate(to_write, 1):
        ok, detail = notion.add_event(ev, account_page_id=apid, group_page_id=gpid)
        wrote += ok
        wfail += (not ok)
        time.sleep(0.35)                  # steady ~2.5/s — under Notion's ceiling
        if i % 250 == 0:
            print(f"  ...written {i}/{len(to_write)} (failed {wfail})", flush=True)

    arch = 0
    for pid in to_archive:
        r = notion._send("PATCH", f"https://api.notion.com/v1/pages/{pid}", json={"archived": True})
        arch += (r.status_code == 200)
        time.sleep(0.35)

    print(f"\nDONE: wrote {wrote}/{len(to_write)} (failed {wfail}), archived {arch}/{len(to_archive)}",
          flush=True)


if __name__ == "__main__":
    main()
