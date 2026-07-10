# Telegram capture bot — private, shadow mode (Phase 1)

Reads messages in your client group(s), classifies `+` / `-` / sick notes, and
logs them to `events.jsonl`. **It writes nothing to Notion and posts nothing
into the groups** — this phase exists to measure how workers actually phrase
things before we trust any auto-write. See `../telegram-capture-plan.md`.

"Private" means: it only acts on chats you allow-list, only obeys commands from
you (the owner), isn't published anywhere, and keeps its token in `.env`.

---

## 1. Create the bot (Telegram, one-time)

In Telegram, open a chat with **@BotFather**:

1. Send `/newbot`, choose a name and a username (must end in `bot`).
2. BotFather replies with an **API token** — copy it.
3. **Disable privacy mode so the bot can see normal messages** (critical — by
   default it only sees `/commands`):
   - `/setprivacy` → pick your bot → **Disable**.
4. *(optional, tightens it)* `/setjoingroups` → **Enable** (you need it in
   groups) and don't publish the bot anywhere.

## 2. Configure

```
cd telegram-bot
copy .env.example .env        # PowerShell:  Copy-Item .env.example .env
```

Edit `.env`:
- `TELEGRAM_BOT_TOKEN` = the token from BotFather.
- Leave `OWNER_USER_ID` and `ALLOWED_CHAT_IDS` empty for now.
- `ANTHROPIC_API_KEY` = optional; without it the bot still runs (rules only, and
  free-text notes get marked `other` for manual review).

## 3. Install and run

```
py -m pip install -r requirements.txt
py bot.py
```

## 4. Discover the ids (first run)

With the bot running:

1. Message the bot **`/whoami`** in a private chat → it replies with **your**
   `user_id`. Put that in `.env` as `OWNER_USER_ID`.
2. Add the bot to **one** pilot client group. In that group send **`/chatid`**
   (as the owner) → it replies with the group's `chat_id` (a negative number).
   Put that in `ALLOWED_CHAT_IDS`.
3. Stop the bot (Ctrl+C) and start it again to load the new values.

Now the bot ignores every chat except the allow-listed group, and every message
in that group is classified and appended to `events.jsonl`.

## 5. What a log line looks like

```json
{"captured_at":"2026-07-10T08:01:22Z","chat_id":-1001234567890,"chat_title":"Client X",
 "user_id":111,"username":"vpetrov","full_name":"Viktor Petrov","text":"+",
 "intent":"clock_in","confidence":0.99,"source":"rule","dates":[]}
```

Review `events.jsonl` after a few real days to see how accurate the classifier
is and how workers phrase sick notes. That evidence drives Phase 2 (the Notion
"Pending Inbox" + one-tap confirmation) and Phase 3 (auto-write of
high-confidence records).

---

## Notes / safety

- The bot opens the group **read-only** in spirit: it never edits or deletes
  anything, and (in shadow mode) never posts into the group.
- The only outbound messages are `/whoami` and `/chatid` replies to the owner.
- To go beyond shadow mode later, set `SHADOW_MODE = False` in `config.py` —
  but don't, until the Notion write + confirmation flow exists.
- Keep `.env` out of version control. Rotate the token in BotFather if it leaks.
