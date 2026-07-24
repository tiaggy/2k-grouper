# Running the bot in Docker

The bot is a **long-polling Telegram bot**: it makes only *outbound* HTTPS calls
(to Telegram, Notion and the AI endpoint) and **listens on no ports**. That keeps
the container attack surface tiny and makes the firewall story simple.

## Quick start

```bash
# 1. Put secrets/toggles in .env (never committed). At minimum:
#    TELEGRAM_BOT_TOKEN, NOTION_TOKEN, NOTION_DB_ID, NOTION_DB_DS_ID,
#    NOTION_CONFIG_DB_ID, NOTION_TRACKED_DB_ID, NOTION_ACCOUNTS_DB_ID,
#    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, AI_ASSIST, OWNER_USER_ID
# 2. Set the timezone for the start-of-day missing sweep / weekend logic:
export TZ=Europe/Kyiv          # or Asia/Baku, etc.

docker compose up -d --build   # build image + start detached
docker compose logs -f         # follow logs
docker compose down            # stop (state survives on the volume)
```

State (`progress.json`, `spool.jsonl`, `events.jsonl`, caches, dashboard id) lives
on the named volume **`bot-state`** mounted at `/data`, so restarts and rebuilds
resume cleanly. The per-group resume markers (`Last Msg ID`) also live in Notion,
so even a fresh volume resumes without reprocessing.

The owner **Restart** button works in Docker: `EXIT_ON_RESTART=1` makes the bot
exit, and `restart: unless-stopped` relaunches it (self-respawning inside a
container would kill PID 1).

---

## ⚠️ The Docker + UFW gotcha

On a Linux host, **Docker bypasses UFW.** When you publish a port
(`-p 8080:8080` or a compose `ports:` entry), Docker writes its own rules into the
`DOCKER` iptables chain, which is consulted **before** UFW's `INPUT` rules. So a
port you *think* UFW is blocking is actually reachable from the internet:

```bash
ufw deny 8080         # looks blocked...
docker run -p 8080:8080 someimage   # ...but the world can reach 8080 anyway
```

### How this project avoids it

**We publish no ports.** The bot needs no inbound connection (long-polling), so
`docker-compose.yml` has **no `ports:` section** and the `Dockerfile` `EXPOSE`s
nothing. With nothing published, Docker inserts no bypassing rules — there is
nothing for UFW to fail to block. This is the safest posture and needs no extra
firewall config.

### If you ever DO publish a port

(e.g. you later add a webhook or a metrics endpoint) don't just `-p 9000:9000`.
Do one of:

1. **Bind to loopback only** and reach it via an SSH tunnel or a reverse proxy on
   the same host:
   ```yaml
   ports:
     - "127.0.0.1:9000:9000"   # not reachable from outside the host
   ```
2. **Install [`ufw-docker`](https://github.com/chaifeng/ufw-docker)** so UFW rules
   actually govern container ports:
   ```bash
   ufw-docker install && ufw route allow proto tcp from any to any port 9000
   ```
3. **Disable Docker's iptables manipulation** (`"iptables": false` in
   `/etc/docker/daemon.json`) and manage all rules yourself — advanced, and it
   breaks inter-container networking unless you set it up carefully.

Prefer option 1 or "publish nothing" whenever possible.

### Outbound firewalling (optional hardening)

UFW's *default* outbound policy is `allow`, which is what the bot needs (Telegram
/ Notion / AI over 443). If you tighten egress, allow at least DNS (53) and HTTPS
(443). No inbound rule is required for the bot itself.
