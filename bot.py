"""Private Telegram attendance capture bot.

Reads messages in the tracked client group(s), classifies them (+ = clock in,
- = clock out/day off, free text → LLM: sick/vacation/holiday/ignore), appends
to events.jsonl, and writes each event to the Notion Capture Log.

Config (owners / ignored users / tracked groups) is the Notion 'Bot Config'
database — the single source of truth, refreshed live. .env holds only secrets
and toggles; an owner adding the bot to a group auto-writes a Tracked Group row.

"Private" = it only captures tracked chats, and only owners can enroll groups or
use the DM commands. The token is kept in .env, never in the code.

Run:  python bot.py
Stop: Ctrl+C
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time

import requests

import config
import classifier
import storage
import tracking
import notion
import notionconfig
import notionaccounts
import missing

API = "https://api.telegram.org/bot{token}/{method}"

# Runtime state.
_debug: bool = False               # mirror log lines to the owner's DM
_ai: bool = False                  # AI-assist mode (light-model fallback)
_started_at: "dt.datetime | None" = None
_should_stop: bool = False         # set by SIGTERM to end the poll loop gracefully
_shutdown_sent: bool = False       # guard so the shutdown DM is sent only once
_restart_requested: bool = False   # owner asked to restart (re-exec in place)
_dashboard_msg: dict = {}          # chat_id -> last dashboard message id (edit in place)

# Config from the Notion 'Bot Config' database (source of truth), refreshed live.
_owners: set = set()               # owner user ids
_ignored: set = set()              # user ids whose messages are skipped
_notion_tracked: set = set()       # tracked group chat ids
_accounts: dict = {}               # user_id -> Telegram Accounts page id
_group_pages: dict = {}            # chat_id -> Tracked Groups page id
_last_cfg_refresh: float = 0.0

_group_last_msg: dict = {}         # chat_id -> last processed Telegram message_id
_progress_dirty: dict = {}         # chat_id -> message_id pending flush to Notion

_MISSING_STATE = config.state_path("missing_state.json")
_PROGRESS_FILE = config.state_path("progress.json")
_SPOOL_FILE = config.state_path("spool.jsonl")


def _redact(s: str) -> str:
    """Strip the bot token out of any string before it can be logged/DM'd.
    requests errors embed the full '.../bot<TOKEN>/method' URL."""
    tok = config.TELEGRAM_BOT_TOKEN
    return s.replace(tok, "<bot-token>") if tok and tok in s else s


def call(method: str, **params):
    url = API.format(token=config.require_token(), method=method)
    try:
        r = requests.post(url, json=params, timeout=65)
        r.raise_for_status()
    except requests.RequestException as exc:
        # Never let the token-bearing URL propagate into logs.
        raise RuntimeError(f"Telegram {method} failed: {_redact(str(exc))}") from None
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data["result"]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _is_weekend(date_str: str | None) -> bool:
    """True if the YYYY-MM-DD date falls on a Saturday or Sunday."""
    try:
        return dt.date.fromisoformat((date_str or "")[:10]).weekday() >= 5
    except Exception:
        return False


def _missing_last_run() -> str:
    try:
        with open(_MISSING_STATE, encoding="utf-8") as fh:
            return json.load(fh).get("last") or ""
    except Exception:
        return ""


def _missing_set_run(date_str: str) -> None:
    try:
        with open(_MISSING_STATE, "w", encoding="utf-8") as fh:
            json.dump({"last": date_str}, fh)
    except Exception as exc:
        log(f"[missing] state save failed: {exc!r}")


# --- resume: per-group last processed message id + Telegram offset -----------

def _save_progress(offset) -> None:
    """Local backup of the Telegram offset + per-group last message id, written
    every poll batch so a crash/restart resumes without reprocessing."""
    try:
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"offset": offset,
                       "last_msg": {str(k): v for k, v in _group_last_msg.items()},
                       "classifier": classifier.dump_state()}, fh)
    except Exception as exc:
        log(f"[progress] save failed: {exc!r}")


def _load_progress() -> "int | None":
    """Seed _group_last_msg from Notion (source of truth) then the local file,
    keeping the max per group. Returns the saved Telegram offset (or None)."""
    global _group_last_msg
    merged: dict = dict(notionconfig.last_msg_ids() or {})
    offset = None
    try:
        with open(_PROGRESS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        offset = data.get("offset")
        for k, v in (data.get("last_msg") or {}).items():
            cid = int(k)
            merged[cid] = max(merged.get(cid, 0), int(v))
        classifier.load_state(data.get("classifier") or {})  # restore +/- pairing state
    except Exception:
        pass
    _group_last_msg = merged
    return offset


def _flush_progress_to_notion() -> None:
    """Persist any advanced last-message ids onto their Tracked Groups rows."""
    if not _progress_dirty:
        return
    for chat_id, msg_id in list(_progress_dirty.items()):
        page_id = _group_pages.get(chat_id)
        if page_id and notionconfig.set_last_msg(page_id, msg_id):
            _progress_dirty.pop(chat_id, None)


def _spool_append(msg: dict) -> None:
    """Durably save a message to process later — used while Notion/AI is down, so
    it's drained from Telegram now (beating the 24h retention) not lost."""
    try:
        with open(_SPOOL_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:
        log(f"[spool] append failed: {exc!r}")


def _spool_count() -> int:
    try:
        with open(_SPOOL_FILE, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except FileNotFoundError:
        return 0
    except Exception:
        return 0


def _spool_drain() -> None:
    """Process every locally-spooled message (retaining its original send time, so
    dates are correct), then clear the spool. Stops early and keeps the remainder
    if a dependency drops again mid-drain."""
    if not os.path.exists(_SPOOL_FILE):
        return
    try:
        with open(_SPOOL_FILE, encoding="utf-8") as fh:
            lines = [l for l in fh if l.strip()]
    except Exception as exc:
        log(f"[spool] read failed: {exc!r}")
        return
    if not lines:
        try:
            os.remove(_SPOOL_FILE)
        except Exception:
            pass
        return
    log(f"[spool] draining {len(lines)} queued message(s)")
    i = 0
    for i, line in enumerate(lines):
        if _block_reason():           # a dependency dropped again — keep the rest
            break
        try:
            handle_message(json.loads(line))
        except Exception as exc:
            log(f"[spool] process failed: {exc!r}")
    else:
        i = len(lines)                # loop finished without break → all consumed
    remaining = lines[i:] if _block_reason() else []
    try:
        if remaining:
            with open(_SPOOL_FILE, "w", encoding="utf-8") as fh:
                fh.writelines(remaining)
        else:
            os.remove(_SPOOL_FILE)
    except Exception as exc:
        log(f"[spool] rewrite failed: {exc!r}")


def _do_restart(offset) -> None:
    """Restart the bot in place. Uses subprocess.Popen (reliable on Windows) to
    spawn a fresh, detached instance, then exits — os.execv is a fallback. Progress
    is persisted first so the new process resumes without reprocessing the trigger."""
    if offset is not None:
        try:
            call("getUpdates", offset=offset, timeout=0)  # confirm updates to Telegram
        except Exception:
            pass
    _save_progress(offset)
    _flush_progress_to_notion()
    print("restarting (owner requested)…", flush=True)
    if config.EXIT_ON_RESTART:
        # Under a supervisor (Docker restart:, systemd, NSSM) — just exit and let it
        # relaunch. Self-respawning inside a container would kill PID 1 and the child.
        os._exit(0)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        kwargs = {"cwd": script_dir, "close_fds": False}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([sys.executable, *sys.argv], **kwargs)
        os._exit(0)  # skip the finally-block shutdown DM: this is a restart, not a stop
    except Exception as exc:
        print(f"restart via Popen failed: {exc!r} — falling back to execv", flush=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])


def maybe_run_missing_sweep() -> None:
    """Sweep every weekday from the last one swept up to the target end day (today
    if past MISSING_SWEEP_HOUR, else yesterday). With MISSING_SWEEP_HOUR=0 the day's
    sweep fires at day start, so every expected worker is marked `missing` up front;
    each `missing` is then replaced by the real record as the worker reports (see the
    archive_missing call in handle_message). A bot that was down catches up each
    missed day. Bounded to the roster lookback. Call only once the backlog is drained
    (records must exist before their day is swept). Each day is persisted."""
    if not (config.MISSING_SWEEP_ENABLED and config.NOTION_ENABLED):
        return
    now = dt.datetime.now()
    today = now.date()
    end = today if now.hour >= config.MISSING_SWEEP_HOUR else today - dt.timedelta(days=1)
    last = _missing_last_run()
    try:
        start = dt.date.fromisoformat(last) + dt.timedelta(days=1) if last else end
    except Exception:
        start = end
    floor = today - dt.timedelta(days=config.MISSING_ROSTER_LOOKBACK_DAYS)
    if start < floor:
        start = floor
    d = start
    while d <= end:
        if d.weekday() < 5:
            try:
                s = missing.sweep(d, log=log)
                log(f"[missing] {s['date']}: wrote {s['written']}, accounted {s['accounted']}, "
                    f"skipped {s['skipped']}, failed {s['failed']}")
            except Exception as exc:
                # Don't claim this (or any later) day as swept — a transient failure
                # (e.g. a Notion timeout) must not permanently skip a day's missing
                # rows. Stop here; the NEXT call retries from this same day.
                log(f"[missing] sweep error {d}: {exc!r} — will retry this day later")
                break
            if s["failed"]:
                # Some rows didn't write even after retries — don't claim the day
                # done; a later retry will only re-attempt the still-missing workers
                # (sweep() only writes for accounts that still lack a record that day).
                log(f"[missing] {d}: {s['failed']} row(s) failed to write — will retry this day later")
                break
        _missing_set_run(d.isoformat())  # only reached once this day fully succeeded
        d += dt.timedelta(days=1)


def send(chat_id: int, text: str, reply_markup: dict | None = None,
         parse_mode: str | None = None) -> None:
    """Best-effort sendMessage; never raises into the poller."""
    params: dict = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    if parse_mode:
        params["parse_mode"] = parse_mode
    try:
        call("sendMessage", **params)
    except Exception as exc:
        print(f"[send] to {chat_id} failed: {exc!r}")


def edit_message(chat_id: int, message_id: int, text: str,
                 reply_markup: dict | None = None, parse_mode: str | None = None) -> None:
    """Edit a message in place (used to refresh /info after a button press)."""
    params: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    if parse_mode:
        params["parse_mode"] = parse_mode
    try:
        call("editMessageText", **params)
    except Exception as exc:
        print(f"[edit_message] failed: {exc!r}")


def answer_callback(callback_query_id: str, text: str | None = None) -> None:
    """Acknowledge a button press so the client stops showing the spinner."""
    params: dict = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    try:
        call("answerCallbackQuery", **params)
    except Exception as exc:
        print(f"[answer_callback] failed: {exc!r}")


def log(msg: str) -> None:
    """Print to the console and, when debug mode is on, mirror to the owner's DM."""
    msg = _redact(msg)  # defense-in-depth: never log/DM the bot token
    print(msg)
    if _debug:
        for _uid in _owners:
            try:
                call("sendMessage", chat_id=_uid, text=msg)
            except Exception as exc:
                print(f"[log->dm] failed: {exc!r}")  # plain print — avoid recursion


def register_commands() -> None:
    """Register the command menu so the DM shows suggestions when you type '/'
    or tap the ☰ menu button. Scoped to the owner's chat so only they see it."""
    cmds = [
        {"command": "dashboard", "description": "Open / refresh the dashboard"},
    ]
    if not _owners:
        try:
            call("setMyCommands", commands=cmds, scope={"type": "all_private_chats"})
        except Exception as exc:
            print(f"[register_commands] failed: {_redact(str(exc))}")
        return
    for _uid in _owners:
        try:
            call("setMyCommands", commands=cmds, scope={"type": "chat", "chat_id": _uid})
        except Exception as exc:
            # e.g. an owner who never opened the bot — skip, don't block the rest.
            print(f"[register_commands] {_uid} skipped: {_redact(str(exc))}")


def _request_stop() -> None:
    global _should_stop
    _should_stop = True


def _notify_owners(text: str) -> None:
    """Best-effort DM to every owner (e.g. an outage / recovery alert)."""
    for _uid in _owners:
        try:
            call("sendMessage", chat_id=_uid, text=text)
        except Exception as exc:
            print(f"[owner-dm] failed: {exc!r}")


# --- health gate: pause processing while Telegram / Notion / AI is down --------
_health_ts: float = 0.0
_telegram_up: bool = True
_notion_up: bool = True
_ai_up: bool = True


def _tg_ok(method: str, **params) -> bool:
    try:
        r = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
                          json=params or None, timeout=10)
        return r.status_code == 200 and r.json().get("ok") is True
    except Exception:
        return False


def _telegram_healthcheck() -> bool:
    """True only if Telegram is reachable (valid token) AND the bot is a member of at
    least one tracked group. Not being in any tracked group means it can capture
    nothing — treated as a Telegram-side outage so the bot pauses and alerts, then
    recovers automatically once it's re-added (getChat starts succeeding)."""
    if not _tg_ok("getMe"):
        return False
    groups = list(allowed_ids())
    if not groups:
        return True  # nothing enrolled yet — not a failure
    return any(_tg_ok("getChat", chat_id=cid) for cid in groups)


def _block_reason() -> "str | None":
    """Cached health check. Returns 'Telegram' / 'Notion' / 'AI' if processing must
    pause (that dependency is unavailable), else None. Telegram is checked first —
    nothing works without it. In AI mode, AI must be up too."""
    global _health_ts, _telegram_up, _notion_up, _ai_up
    now = time.monotonic()
    if now - _health_ts >= config.HEALTH_TTL_SECONDS:
        _health_ts = now
        _telegram_up = _telegram_healthcheck()
        _notion_up = notion.healthcheck() if config.NOTION_ENABLED else True
        _ai_up = classifier.ai_healthcheck() if _ai_on() else True
    if not _telegram_up:
        return "Telegram"
    if config.NOTION_ENABLED and not _notion_up:
        return "Notion"
    if _ai_on() and not _ai_up:
        return "AI"
    return None


def _notify_shutdown() -> None:
    """Best-effort 'bot stopped' DM to the owner, sent once on graceful exit."""
    global _shutdown_sent
    if _shutdown_sent or not _owners:
        return
    _shutdown_sent = True
    for _uid in _owners:
        try:
            call("sendMessage", chat_id=_uid,
                 text="bot stopped — capture is paused until it's restarted.")
        except Exception as exc:
            print(f"[shutdown-dm] failed: {exc!r}")


def allowed_ids() -> set[int]:
    """Tracked group ids — the Notion Bot Config working set (Notion is the
    source of truth; `tracking` is only an offline cache of this set)."""
    return set(_notion_tracked)


def _ai_on() -> bool:
    """AI-assist is effective only when toggled on AND a key is configured."""
    return _ai and bool(config.OPENAI_API_KEY)


def _is_owner(uid) -> bool:
    return uid in _owners


def notify_owners(text: str) -> None:
    for uid in _owners:
        send(uid, text)


def _ensure_account_page(user_id, frm: dict) -> str | None:
    """Return the sender's Telegram Accounts page id, creating the row if new."""
    if user_id is None:
        return None
    pid = _accounts.get(user_id)
    if pid:
        return pid
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
    pid = notionaccounts.get_or_create(user_id, name, frm.get("username"))
    if pid:
        _accounts[user_id] = pid
    return pid


def load_notion_config() -> bool:
    """Refresh owners / ignored / tracked groups / account & group page maps from
    Notion. Falls back to the .env owner so a Notion outage can't lock the owner out."""
    global _owners, _ignored, _notion_tracked, _accounts, _group_pages, _last_cfg_refresh
    _last_cfg_refresh = time.monotonic()
    acc = notionaccounts.load()
    if acc is not None:
        _accounts = acc
    gp = notionconfig.tracked_pages()
    if gp is not None:
        _group_pages = gp
    cfg = notionconfig.load()
    env_owner = {config.OWNER_USER_ID} if config.OWNER_USER_ID else set()
    if cfg is None:
        # Notion unavailable: keep prior state; fall back to .env owner + cached
        # tracked groups so the bot still functions.
        if not _owners:
            _owners = set(env_owner)
        if not _notion_tracked:
            _notion_tracked = tracking.load()
        return False
    _owners = cfg["owners"] or env_owner
    _ignored = cfg["ignored"]
    _notion_tracked = cfg["tracked"]
    tracking.save(_notion_tracked)  # refresh the offline cache
    return True


# --- Membership changes: the bot itself was added to / removed from a chat ---

_JOINED = {"member", "administrator", "creator"}
_LEFT = {"left", "kicked", "restricted"}


def handle_my_chat_member(mcm: dict) -> None:
    chat = mcm.get("chat", {})
    if chat.get("type") not in ("group", "supergroup"):
        return  # ignore private chats / channels — only groups are enrollable
    chat_id = chat.get("id")
    title = chat.get("title")
    actor = mcm.get("from", {})
    actor_id = actor.get("id")
    new_status = (mcm.get("new_chat_member") or {}).get("status")

    if new_status in _JOINED:
        # Only an owner may enroll a group — writes a Tracked Group row to the
        # Notion Bot Config (the source of truth) and updates the local cache.
        if _is_owner(actor_id):
            if chat_id in _notion_tracked:
                log(f"[enroll] chat_id={chat_id} already tracked — not adding again")
            else:
                _notion_tracked.add(chat_id)
                tracking.save(_notion_tracked)
                notionconfig.add_tracked_group(chat_id, title)
                log(f"[enroll] now tracking chat_id={chat_id} title={title!r} (added by owner)")
        else:
            who = actor.get("username") or actor_id
            log(f"[enroll] bot added to chat_id={chat_id} by non-owner {who} — NOT tracking")

    elif new_status in _LEFT:
        # Bot removed/kicked. Drop it locally and untrack it in Notion (uncheck
        # Enabled on the Tracked Groups row — the row, its Client relation, and the
        # Capture Log history stay intact; capture just stops).
        if chat_id in _notion_tracked:
            _notion_tracked.discard(chat_id)
            _group_pages.pop(chat_id, None)
            tracking.save(_notion_tracked)
            ok = notionconfig.disable_tracked_group(chat_id)
            log(f"[enroll] removed from chat_id={chat_id} title={title!r} — untracked "
                + ("(disabled its Notion row)" if ok else "locally (Notion row not found/updated)"))


# --- Owner commands (private DM with the bot) ------------------------------

def _uptime() -> str:
    if _started_at is None:
        return "?"
    secs = int((dt.datetime.now(dt.timezone.utc) - _started_at).total_seconds())
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, s = divmod(secs, 60)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m", f"{s}s") if p)


def _events_summary() -> "tuple[int, dict]":
    import json
    from collections import Counter
    intents: Counter = Counter()
    total = 0
    try:
        with open(config.EVENTS_LOG, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                total += 1
                try:
                    intents[json.loads(line).get("intent", "?")] += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return total, dict(intents)


_INTENT_LABEL = {
    "clock_in": "🟢 in",
    "clock_out": "🔴 out",
    "sick": "🤒 sick",
    "vacation": "🏖 vac",
    "public_holiday": "🎌 holiday",
    "day_off": "🌙 off",
    "other": "❔ other",
}


def _notion_url(db_id: str) -> str:
    return f"https://www.notion.so/{db_id.replace('-', '')}" if db_id else ""


def build_info() -> str:
    """The dashboard card (parse_mode=HTML): uptime + Notion links. AI-assist /
    debug state live on the buttons; toggle them there."""
    lines = ["🤖 <b>2K capture bot</b>", "", f"⏱ uptime — <b>{_uptime()}</b>", ""]
    links = []
    for name, db_id in (("Tracked Groups", config.NOTION_TRACKED_DB_ID),
                        ("Bot Config", config.NOTION_CONFIG_DB_ID),
                        ("Capture Log", config.NOTION_DB_ID)):
        if db_id:
            links.append(f'<a href="{_notion_url(db_id)}">{name}</a>')
    if links:
        lines.append("🔗 " + " · ".join(links))
    return "\n".join(lines)


def info_keyboard() -> dict:
    """Inline keyboard — buttons show current AI-assist / debug state and toggle
    it on click; plus a restart button."""
    return {"inline_keyboard": [
        [{"text": f"AI-assist: {'ON' if _ai else 'OFF'}", "callback_data": "ai_toggle"}],
        [{"text": f"Debug: {'ON' if _debug else 'OFF'}", "callback_data": "debug_toggle"}],
        [{"text": "🔄 Restart bot", "callback_data": "restart"}],
    ]}


_DASH_FILE = config.state_path("dashboard_msg.json")


def _load_dashboards() -> dict:
    try:
        with open(_DASH_FILE, encoding="utf-8") as f:
            return {int(k): int(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


def _save_dashboards() -> None:
    try:
        with open(_DASH_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _dashboard_msg.items() if v is not None}, f)
    except Exception as exc:
        print(f"[dashboard] save failed: {exc!r}")


def show_dashboard(chat_id) -> None:
    """Show the dashboard, keeping a single one per chat: edit the last one if it
    still exists, otherwise send a fresh one and delete the old."""
    text, kb = build_info(), info_keyboard()
    old = _dashboard_msg.get(chat_id)
    if old is not None:
        try:
            call("editMessageText", chat_id=chat_id, message_id=old, text=text,
                 reply_markup=kb, parse_mode="HTML")
            return
        except Exception as exc:
            if "not modified" in str(exc):
                return
            # message deleted / too old → send a fresh one below
    try:
        res = call("sendMessage", chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        print(f"[dashboard] failed: {_redact(str(exc))}")
        return
    new_id = (res or {}).get("message_id")
    if old is not None and old != new_id:  # a new one was sent → remove the old
        try:
            call("deleteMessage", chat_id=chat_id, message_id=old)
        except Exception:
            pass
    _dashboard_msg[chat_id] = new_id
    _save_dashboards()


def handle_callback_query(cq: dict) -> None:
    """Handle inline-button presses (owner only)."""
    global _debug, _ai, _restart_requested, _dashboard_msg
    cq_id = cq.get("id")
    frm = cq.get("from", {})
    if frm.get("id") not in _owners:
        answer_callback(cq_id, "Not allowed.")
        return

    data = cq.get("data")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    if data == "restart":
        answer_callback(cq_id, "Restarting…")
        send(chat_id, "🔄 restarting… (back in a few seconds)")
        _restart_requested = True
        return
    if data == "debug_toggle":
        _debug = not _debug
        answer_callback(cq_id, f"Debug is now {'ON' if _debug else 'off'}")
    elif data == "ai_toggle":
        _ai = not _ai
        note = "" if config.OPENAI_API_KEY else " (needs an OpenAI key to take effect)"
        answer_callback(cq_id, f"AI-assist is now {'on' if _ai else 'off'}{note}")
    else:
        answer_callback(cq_id)
        return

    if chat_id and message_id:
        _dashboard_msg[chat_id] = message_id  # track the dashboard we're editing
        _save_dashboards()
        edit_message(chat_id, message_id, build_info(),
                     reply_markup=info_keyboard(), parse_mode="HTML")


def handle_owner_command(chat_id: int, text: str) -> bool:
    """Only one command: /dashboard (also /start). Everything is on the dashboard
    buttons. Any owner /command just opens/refreshes the dashboard."""
    if not text.strip().startswith("/"):
        return False
    show_dashboard(chat_id)
    return True


# --- Normal group messages -------------------------------------------------

def handle_message(msg: dict) -> None:
    chat = msg.get("chat", {})
    frm = msg.get("from", {})
    chat_id = chat.get("id")
    user_id = frm.get("id")
    text = msg.get("text") or msg.get("caption") or ""

    # Private chat with the bot: only owners are served (the /dashboard command).
    # Delete the owner's command message so the chat stays just the dashboard.
    if chat.get("type") == "private":
        if _is_owner(user_id) and handle_owner_command(chat_id, text):
            try:
                call("deleteMessage", chat_id=chat_id, message_id=msg.get("message_id"))
            except Exception:
                pass
        return

    # Access control: capture only tracked chats. Log ids of ignored chats so a
    # missing enrollment is easy to spot.
    if chat_id not in allowed_ids():
        log(f"[ignored] chat_id={chat_id} title={chat.get('title')!r} (not tracked)")
        return
    # Resume guard: Telegram message ids increase per chat, so skip anything at or
    # below the last one we processed (a redelivery after a crash/restart). Advance
    # the marker for every message we see here (even ones we skip below).
    mid = msg.get("message_id")
    if mid is not None:
        if mid <= _group_last_msg.get(chat_id, 0):
            return
        _group_last_msg[chat_id] = mid
        _progress_dirty[chat_id] = mid
    if user_id in _ignored:
        return  # ignored user (manager/owner/etc.) — no account, don't capture
    if not text.strip():
        return  # stickers, photos w/o caption, service messages, etc.
    account_pid = _ensure_account_page(user_id, frm)  # register/resolve the account
    group_pid = _group_pages.get(chat_id)

    # Use the message's own send time (Telegram `date`, Unix seconds), NOT now — so
    # a message processed late (resume/backlog) still lands on the day it was sent
    # and its +/- resolves against the right day. Falls back to now if absent.
    sent = msg.get("date")
    try:
        ts = dt.datetime.fromtimestamp(sent, dt.timezone.utc) if sent else dt.datetime.now(dt.timezone.utc)
    except Exception:
        ts = dt.datetime.now(dt.timezone.utc)
    base = {
        "captured_at": ts.isoformat(),
        "chat_id": chat_id,
        "chat_title": chat.get("title"),
        "user_id": user_id,
        "username": frm.get("username"),
        "full_name": " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x),
        "message_id": msg.get("message_id"),
    }
    who = base["full_name"] or base["username"] or str(user_id)

    # A single message can yield several signals (e.g. "+(10)\n-(19)") — one row
    # each; a signal spanning several days (e.g. a vacation range) → one row per day.
    msg_date = base["captured_at"][:10]
    cleared: set = set()   # days we've already cleared a stale `missing` for
    days_written: set = set()   # days that got a real record from this message
    for s in classifier.classify(text, ai=_ai_on(), key=user_id, ts=ts):
        if s.intent == classifier.IGNORE:
            log(f"[ignored-casual  ] {who}: {(s.text or text)[:50]!r}")
            continue
        for day in classifier.expand_dates(s, msg_date):
            if _is_weekend(day):
                log(f"[weekend-skip {day}] {who}: {(s.text or text)[:40]!r}")
                continue
            event = dict(base)
            event.update({
                "text": s.text or text,
                "intent": s.intent,
                "confidence": s.confidence,
                "source": s.source,
                "dates": [day] if day else [],
            })
            storage.record_event(event)
            log(f"[{s.intent:13s} {s.confidence:.2f} {s.source:4s}] {who} {day}: {(s.text or text)[:50]!r}")
            if config.NOTION_ENABLED:
                # A real record supersedes a previously-swept `missing` that day.
                if day and day not in cleared:
                    removed = notion.archive_missing(account_pid, day)
                    if removed:
                        log(f"[missing-cleared {day}] {who}: removed {removed} stale missing row(s)")
                    cleared.add(day)
                ok, detail = notion.add_event(event, account_page_id=account_pid, group_page_id=group_pid)
                if not ok:
                    log(f"[notion] write failed: {detail}")
                elif day:
                    days_written.add(day)

    # A day left with an ambiguous set of records (not a lone record / clean
    # clock_in+clock_out pair) gets all its records marked `unresolved`.
    if config.NOTION_ENABLED:
        for day in days_written:
            flipped = notion.enforce_day_unresolved(account_pid, day)
            if flipped:
                log(f"[unresolved-day {day}] {who}: flipped {flipped} record(s) to unresolved")


def main() -> None:
    global _debug, _ai, _started_at
    config.require_token()
    _debug = config.DEBUG_TO_OWNER
    _ai = config.AI_ASSIST
    _started_at = dt.datetime.now(dt.timezone.utc)

    cfg_ok = load_notion_config()  # owners / ignored / tracked from Notion
    _dashboard_msg.update(_load_dashboards())  # so restart edits the same dashboard

    print("Shadow-mode capture bot starting.")
    print(f"  config source : {'Notion Bot Config' if cfg_ok else '.env fallback (Notion config unavailable)'}")
    print(f"  owners        : {sorted(_owners) or '(none)'}")
    print(f"  ignored users : {sorted(_ignored) or '(none)'}")
    print(f"  tracked groups: {sorted(allowed_ids()) or '(none)'}")
    print(f"  ai-assist     : {'ON (' + config.OPENAI_MODEL + ')' if _ai_on() else ('off (no API key)' if _ai else 'off — rules only')}")
    print(f"  notion        : {'ON (db ' + config.NOTION_DB_ID + ')' if config.NOTION_ENABLED else 'off'}")
    print(f"  debug to DM   : {'ON' if _debug else 'off'}")
    print("Add the bot to a group as an owner to start tracking it. Ctrl+C to stop.\n")

    # Register the DM command menu (suggestions when typing '/').
    register_commands()

    # On (re)start, show the dashboard to each owner.
    for _uid in _owners:
        show_dashboard(_uid)

    offset = _load_progress()  # resume: last Telegram offset + per-group last msg id
    if _group_last_msg:
        print(f"  resume        : {len(_group_last_msg)} group(s), offset={offset}")
    degraded: "str | None" = None    # 'Telegram' / 'Notion' / 'AI' if a dep is down
    since: float = 0.0
    alerted: bool = False
    while not _should_stop:
        # Health gate. When Notion or AI (in AI mode) is down we KEEP fetching from
        # Telegram and spool messages to a durable local file — so nothing is lost to
        # Telegram's 24h retention — and process them on recovery. When TELEGRAM
        # itself is down we can neither fetch nor DM; we just wait and re-check.
        reason = _block_reason()
        if reason and degraded != reason:
            degraded, since, alerted = reason, time.monotonic(), False
            log(f"[degraded] {reason} unavailable")
            if reason == "Notion":
                _notify_owners("⚠️ Notion is unavailable — incoming messages are being saved "
                               "locally and will be processed automatically once it recovers.")
                alerted = True
            elif reason == "Telegram":
                # Best-effort: the DM reaches owners if the API is up (e.g. the bot
                # was removed from the groups); it just fails silently if it's not.
                _notify_owners("⚠️ Capture paused — Telegram is unreachable, or the bot isn't in "
                               "any tracked group. It resumes automatically once fixed.")
                alerted = True
            # AI: alert only after an hour (handled below).
        if reason == "AI" and not alerted and \
                time.monotonic() - since >= config.AI_ALERT_AFTER_SECONDS:
            _notify_owners("⚠️ AI has been unavailable for over an hour — messages are being saved "
                           "locally (AI mode). They'll be processed once AI recovers, or turn AI "
                           "mode off to process them now with rules only.")
            alerted = True
        if not reason and degraded:
            queued = _spool_count()
            if degraded == "Telegram":
                _notify_owners("✅ Telegram connection restored — capture resumed.")
            elif alerted:
                _notify_owners(f"✅ {degraded} is back — processing {queued} saved message(s).")
            log(f"[recovered] {degraded} back — {queued} spooled")
            degraded, alerted = None, False

        if reason == "Telegram":
            # No point calling getUpdates — Telegram is unreachable. Wait and re-check.
            time.sleep(config.PAUSE_RETRY_SECONDS)
            continue

        # Config refresh, spool drain and missing sweep only make sense when healthy.
        if not reason:
            if time.monotonic() - _last_cfg_refresh >= config.CONFIG_REFRESH_SECONDS:
                load_notion_config()
                _flush_progress_to_notion()
            _spool_drain()  # process anything saved during a past outage

        try:
            updates = call(
                "getUpdates",
                offset=offset,
                timeout=50,  # long-poll: Telegram holds the request open
                allowed_updates=["message", "my_chat_member", "callback_query"],
            )
        except Exception as exc:
            log(f"[getUpdates] {exc!r} — retrying in 3s")
            time.sleep(3)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "my_chat_member" in upd:
                    handle_my_chat_member(upd["my_chat_member"])
                elif "callback_query" in upd:
                    handle_callback_query(upd["callback_query"])  # restart/toggles work while degraded
                elif "message" in upd:
                    if reason:
                        _spool_append(upd["message"])  # save now, process on recovery
                    else:
                        handle_message(upd["message"])
            except Exception as exc:
                log(f"[update] {exc!r}")

        if updates:
            _save_progress(offset)  # local backup every batch (offset + last msg ids)
        elif not reason:
            # Idle + healthy: backlog drained → safe to run the missing sweep.
            maybe_run_missing_sweep()

        if _restart_requested:
            _do_restart(offset)


if __name__ == "__main__":
    # SIGTERM (e.g. NSSM/service stop) → graceful shutdown, same as Ctrl+C.
    try:
        signal.signal(signal.SIGTERM, lambda *_: _request_stop())
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        _flush_progress_to_notion()  # persist last-msg ids so a restart resumes cleanly
        _notify_shutdown()
