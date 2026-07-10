"""Private, shadow-mode Telegram capture bot.

Reads every message in the tracked client group(s), classifies it
(+ = clock in, - = clock out, free text = sick/vacation/other), and appends the
result to events.jsonl. It writes NOTHING to Notion and posts NOTHING back into
the groups (see config.SHADOW_MODE).

Enrollment is dynamic: when the OWNER (config.OWNER_USER_ID) adds the bot to a
group, the bot starts tracking that group; when the bot is removed, it stops.
Groups pinned statically in ALLOWED_CHAT_IDS are always tracked too.

"Private" = it only captures chats it tracks, and only the owner can enroll new
groups by adding the bot. The token is kept in .env, never in the code.

Run:  python bot.py
Stop: Ctrl+C
"""
from __future__ import annotations

import datetime as dt
import html
import signal
import time

import requests

import config
import classifier
import storage
import tracking
import notion

API = "https://api.telegram.org/bot{token}/{method}"

# Loaded once at startup, mutated at runtime as groups are added/removed.
_tracked: dict[str, dict] = {}

# Runtime state.
_debug: bool = False               # mirror log lines to the owner's DM
_ai: bool = False                  # AI-assist mode (light-model fallback)
_started_at: "dt.datetime | None" = None
_should_stop: bool = False         # set by SIGTERM to end the poll loop gracefully
_shutdown_sent: bool = False       # guard so the shutdown DM is sent only once


def call(method: str, **params):
    url = API.format(token=config.require_token(), method=method)
    r = requests.post(url, json=params, timeout=65)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data["result"]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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
    print(msg)
    if _debug and config.OWNER_USER_ID:
        try:
            call("sendMessage", chat_id=config.OWNER_USER_ID, text=msg)
        except Exception as exc:
            print(f"[log->dm] failed: {exc!r}")  # plain print — avoid recursion


def register_commands() -> None:
    """Register the command menu so the DM shows suggestions when you type '/'
    or tap the ☰ menu button. Scoped to the owner's chat so only they see it."""
    cmds = [
        {"command": "info", "description": "Status: tracked groups & capture counts"},
        {"command": "ai", "description": "AI-assist for tricky messages — /ai on|off"},
        {"command": "debug", "description": "Mirror my logs to this chat — /debug on|off"},
        {"command": "help", "description": "Show available commands"},
    ]
    try:
        scope = {"type": "chat", "chat_id": config.OWNER_USER_ID} if config.OWNER_USER_ID else {"type": "all_private_chats"}
        call("setMyCommands", commands=cmds, scope=scope)
    except Exception as exc:
        print(f"[register_commands] failed: {exc!r}")


def _request_stop() -> None:
    global _should_stop
    _should_stop = True


def _notify_shutdown() -> None:
    """Best-effort 'bot stopped' DM to the owner, sent once on graceful exit."""
    global _shutdown_sent
    if _shutdown_sent or not config.OWNER_USER_ID:
        return
    _shutdown_sent = True
    try:
        call("sendMessage", chat_id=config.OWNER_USER_ID,
             text="bot stopped — capture is paused until it's restarted.")
    except Exception as exc:
        print(f"[shutdown-dm] failed: {exc!r}")


def allowed_ids() -> set[int]:
    """Static (.env) ids unioned with dynamically tracked ids."""
    return config.ALLOWED_CHAT_IDS | tracking.ids(_tracked)


def _ai_on() -> bool:
    """AI-assist is effective only when toggled on AND a key is configured."""
    return _ai and bool(config.OPENAI_API_KEY)


# --- Membership changes: the bot itself was added to / removed from a chat ---

_JOINED = {"member", "administrator", "creator"}
_LEFT = {"left", "kicked", "restricted"}


def handle_my_chat_member(mcm: dict) -> None:
    chat = mcm.get("chat", {})
    chat_id = chat.get("id")
    title = chat.get("title")
    actor = mcm.get("from", {})
    actor_id = actor.get("id")
    new_status = (mcm.get("new_chat_member") or {}).get("status")

    if new_status in _JOINED:
        # Only the owner may enroll a group. Anyone else adding the bot is ignored
        # so a random person can't start tracking a chat.
        if config.OWNER_USER_ID and actor_id == config.OWNER_USER_ID:
            _tracked[str(chat_id)] = {
                "title": title,
                "added_by": actor_id,
                "added_at": now_iso(),
            }
            tracking.save(_tracked)
            log(f"[enroll] now tracking chat_id={chat_id} title={title!r} (added by owner)")
        else:
            who = actor.get("username") or actor_id
            log(f"[enroll] bot added to chat_id={chat_id} by non-owner {who} — NOT tracking")

    elif new_status in _LEFT:
        # Removed/kicked/lost access: stop tracking (owner or not — the bot is gone).
        if str(chat_id) in _tracked:
            del _tracked[str(chat_id)]
            tracking.save(_tracked)
            log(f"[enroll] removed from chat_id={chat_id} title={title!r} — stopped tracking")
        elif chat_id in config.ALLOWED_CHAT_IDS:
            log(f"[enroll] removed from chat_id={chat_id}, but it is pinned in ALLOWED_CHAT_IDS "
                  f"(.env) — still counted as allowed until you unpin it")


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
    "other": "❔ other",
}


def build_info() -> str:
    """HTML-formatted status card (parse_mode=HTML)."""
    total, intents = _events_summary()
    auto = tracking.ids(_tracked)
    pinned = config.ALLOWED_CHAT_IDS

    rows = []
    for cid in sorted(auto | pinned):
        title = html.escape((_tracked.get(str(cid)) or {}).get("title") or "?")
        tags = "+".join(t for t, on in (("auto", cid in auto), ("pinned", cid in pinned)) if on)
        rows.append(f"  • <code>{cid}</code> — {title} <i>[{tags}]</i>")
    groups = "\n".join(rows) or "  <i>(none — add me to a group)</i>"

    by_intent = " · ".join(f"{_INTENT_LABEL.get(k, k)} {v}" for k, v in sorted(intents.items())) or "—"

    mode = "🟢 LIVE → Notion" if config.NOTION_ENABLED else "🟡 SHADOW (local only)"
    debug = "🟢 ON" if _debug else "⚪️ off"
    notion_state = "🟢 writing to Capture Log" if config.NOTION_ENABLED else "⚪️ off"
    if not config.OPENAI_API_KEY:
        ai_state = "⚪️ off (no API key)"
    elif _ai:
        ai_state = f"🟢 on ({html.escape(config.OPENAI_MODEL)})"
    else:
        ai_state = "⚪️ off (rules only)"

    return (
        "🤖 <b>2K capture bot</b>\n"
        "\n"
        f"⏱ uptime — <b>{_uptime()}</b>\n"
        f"⚙️ mode — {mode}\n"
        f"🗒 Notion — {notion_state}\n"
        f"🤝 AI-assist — {ai_state}\n"
        f"🐛 debug → DM — {debug}\n"
        f"👤 owner — <code>{config.OWNER_USER_ID}</code>\n"
        "\n"
        f"📍 <b>tracked groups</b>\n{groups}\n"
        "\n"
        f"📊 <b>captured</b> — {total}\n"
        f"   {by_intent}"
    )


def info_keyboard() -> dict:
    """Inline keyboard for the /info card — toggle AI-assist and debug mode."""
    ai = "🤝 AI-assist: OFF" if _ai else "🤝 AI-assist: ON"
    dbg = "🔕 Debug: OFF" if _debug else "🔔 Debug: ON"
    return {"inline_keyboard": [
        [{"text": ai, "callback_data": "ai_toggle"}],
        [{"text": dbg, "callback_data": "debug_toggle"}],
    ]}


def handle_callback_query(cq: dict) -> None:
    """Handle inline-button presses (owner only)."""
    global _debug, _ai
    cq_id = cq.get("id")
    frm = cq.get("from", {})
    if not (config.OWNER_USER_ID and frm.get("id") == config.OWNER_USER_ID):
        answer_callback(cq_id, "Not allowed.")
        return

    data = cq.get("data")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

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
        edit_message(chat_id, message_id, build_info(),
                     reply_markup=info_keyboard(), parse_mode="HTML")


def handle_owner_command(chat_id: int, text: str) -> bool:
    """Handle a /command from the owner in a private chat.
    Returns True if the text was a command (handled or unknown)."""
    global _debug, _ai
    t = text.strip()
    if not t.startswith("/"):
        return False
    head = t.split()[0]
    cmd = head.lower().split("@")[0]
    arg = t[len(head):].strip().lower()

    if cmd == "/info":
        send(chat_id, build_info(), reply_markup=info_keyboard(), parse_mode="HTML")
    elif cmd == "/debug":
        if arg in ("on", "1", "true", "yes"):
            _debug = True
            send(chat_id, "debug to DM is now ON — I'll mirror log lines here. /debug off to stop.")
        elif arg in ("off", "0", "false", "no"):
            _debug = False
            send(chat_id, "debug to DM is now off.")
        else:
            send(chat_id, f"debug to DM is currently {'ON' if _debug else 'off'}. Use /debug on or /debug off.")
    elif cmd == "/ai":
        note = "" if config.OPENAI_API_KEY else " (note: no OpenAI key set, so it has no effect yet)"
        if arg in ("on", "1", "true", "yes"):
            _ai = True
            send(chat_id, f"AI-assist is now ON — the light model will handle messages the rules can't.{note}")
        elif arg in ("off", "0", "false", "no"):
            _ai = False
            send(chat_id, "AI-assist is now off — rules only.")
        else:
            send(chat_id, f"AI-assist is currently {'on' if _ai else 'off'}. Use /ai on or /ai off.")
    elif cmd in ("/start", "/help"):
        send(
            chat_id,
            "2K capture bot — owner commands\n"
            "\n"
            "/info — status: tracked groups & capture counts\n"
            "/ai on — use the light model for tricky messages\n"
            "/ai off — rules only\n"
            "/debug on — mirror my logs to this chat\n"
            "/debug off — stop mirroring\n"
            "/help — show this message\n"
            "\n"
            "Tip: tap the ☰ menu (or type /) to pick a command.\n"
            "To track a new group, just add me to it — I start capturing automatically.",
        )
    else:
        send(chat_id, "Unknown command. Try /info or /help.")
    return True


# --- Normal group messages -------------------------------------------------

def handle_message(msg: dict) -> None:
    chat = msg.get("chat", {})
    frm = msg.get("from", {})
    chat_id = chat.get("id")
    user_id = frm.get("id")
    text = msg.get("text") or msg.get("caption") or ""

    # Private chat with the bot: only the owner is served (commands like /info,
    # /debug). All other private chatter is ignored.
    if chat.get("type") == "private":
        if config.OWNER_USER_ID and user_id == config.OWNER_USER_ID:
            handle_owner_command(chat_id, text)
        return

    # Access control: capture only tracked chats. Log ids of ignored chats so a
    # missing enrollment is easy to spot.
    if chat_id not in allowed_ids():
        log(f"[ignored] chat_id={chat_id} title={chat.get('title')!r} (not tracked)")
        return
    if not text.strip():
        return  # stickers, photos w/o caption, service messages, etc.

    base = {
        "captured_at": now_iso(),
        "chat_id": chat_id,
        "chat_title": chat.get("title"),
        "user_id": user_id,
        "username": frm.get("username"),
        "full_name": " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x),
        "message_id": msg.get("message_id"),
    }
    who = base["full_name"] or base["username"] or str(user_id)

    # A single message can yield several signals (e.g. "+(10)\n-(19)") — one row each.
    for s in classifier.classify(text, ai=_ai_on()):
        event = dict(base)
        event.update({
            "text": s.text or text,
            "intent": s.intent,
            "confidence": s.confidence,
            "source": s.source,
            "time": s.time,
            "dates": s.dates,
        })
        storage.record_event(event)
        tlabel = f" @{s.time}" if s.time else ""
        log(f"[{s.intent:13s} {s.confidence:.2f} {s.source:4s}] {who}: {(s.text or text)[:50]!r}{tlabel}")
        if config.NOTION_ENABLED:
            ok, detail = notion.add_event(event)
            if not ok:
                log(f"[notion] write failed: {detail}")


def main() -> None:
    global _tracked, _debug, _ai, _started_at
    config.require_token()
    _tracked = tracking.load()
    _debug = config.DEBUG_TO_OWNER
    _ai = config.AI_ASSIST
    _started_at = dt.datetime.now(dt.timezone.utc)

    print("Shadow-mode capture bot starting.")
    print(f"  pinned (.env) : {sorted(config.ALLOWED_CHAT_IDS) or '(none)'}")
    print(f"  tracked (auto): {sorted(tracking.ids(_tracked)) or '(none yet)'}")
    print(f"  owner user_id : {config.OWNER_USER_ID or '(NOT set — auto-enroll disabled; only pinned chats are captured)'}")
    print(f"  ai-assist     : {'ON (' + config.OPENAI_MODEL + ')' if _ai_on() else ('off (no API key)' if _ai else 'off — rules only')}")
    print(f"  notion        : {'ON (db ' + config.NOTION_DB_ID + ')' if config.NOTION_ENABLED else 'off'}")
    print(f"  debug to DM   : {'ON' if _debug else 'off'}")
    print(f"  logging to    : {config.EVENTS_LOG}")
    print("Add the bot to a group as the owner to start tracking it. Ctrl+C to stop.\n")

    # Register the DM command menu (suggestions when typing '/').
    register_commands()

    # One-time heartbeat so the owner knows the bot (re)started and DM works.
    if config.OWNER_USER_ID:
        send(config.OWNER_USER_ID, "bot started — send /info for status." + (" (debug to DM is ON)" if _debug else ""))

    offset = None
    while not _should_stop:
        try:
            updates = call(
                "getUpdates",
                offset=offset,
                timeout=50,  # long-poll: Telegram holds the request open
                # Must list my_chat_member explicitly — naming allowed_updates
                # otherwise suppresses membership events.
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
                    handle_callback_query(upd["callback_query"])
                elif "message" in upd:
                    handle_message(upd["message"])
            except Exception as exc:
                log(f"[update] {exc!r}")


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
        _notify_shutdown()
