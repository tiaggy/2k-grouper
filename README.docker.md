# Running this project in Docker

Two independent services, one `docker-compose.yml`:

| Service | What it is | Network posture |
|---|---|---|
| `bot` | The long-polling Telegram bot | **Outbound only** — publishes no ports |
| `dashboard` | The read-only attendance web dashboard | Publishes **one** port, bound to loopback by default |

They share the same `.env` (Notion/secrets) but run as separate images, processes,
and volumes — restarting or rebuilding one never touches the other.

## Quick start

```bash
# 1. Put secrets/toggles in .env (never committed). At minimum:
#    TELEGRAM_BOT_TOKEN, NOTION_TOKEN, NOTION_DB_ID, NOTION_DB_DS_ID,
#    NOTION_CONFIG_DB_ID, NOTION_TRACKED_DB_ID, NOTION_ACCOUNTS_DB_ID,
#    NOTION_APPROVALS_DB_ID, OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
#    AI_ASSIST, OWNER_USER_ID, DASHBOARD_APPROVE_TOKEN
# 2. Set the timezone (drives the bot's start-of-day sweep AND the dashboard's
#    week boundaries):
export TZ=Europe/Kyiv          # or Asia/Baku, etc.

docker compose up -d --build   # builds + starts BOTH services, detached
docker compose logs -f         # follow logs (both); add a service name to filter
docker compose down            # stop both (state survives on their volumes)

# Just one service:
docker compose up -d --build bot
docker compose up -d --build dashboard
```

State lives on named volumes, one per service (`bot-state`, `dashboard-state`),
so restarts and rebuilds resume cleanly. The bot's Restart button and resume
markers work the same as always — see the main README for that.

The dashboard, once up, is reachable at **`http://127.0.0.1:8000`** on the host
it's running on (see below for reaching it from elsewhere).

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

### The bot: avoids this entirely

The bot needs no inbound connection (long-polling), so it publishes **no ports**
at all — `EXPOSE`s nothing, no `ports:` entry. With nothing published, Docker
inserts no bypassing rules; there's nothing for UFW to fail to block.

### The dashboard: publishes a port, so this DOES apply

The dashboard is a real web page — it has to listen somewhere. `docker-compose.yml`
binds it to **loopback only**:

```yaml
ports:
  - "127.0.0.1:8000:8000"   # NOT reachable from outside this host, UFW or not
```

This is deliberate and is the safe default: from a fresh `docker compose up`, the
dashboard is reachable only from processes running on the same machine — no
firewall rule needed, because there's nothing exposed to the network to firewall
in the first place.

### Reaching the dashboard from elsewhere

Pick one, depending on who needs access:

**Just you, occasionally (simplest, no server changes):**
```bash
ssh -L 8000:localhost:8000 you@your-vps
# then open http://localhost:8000 on your own machine
```

**Your team, over the internet (a real reverse proxy with TLS):**
Put [Caddy](https://caddyserver.com/) or nginx in front, terminating TLS on 443/80
(which you *do* open in UFW) and proxying to `127.0.0.1:8000`. A minimal Caddyfile:
```
attendance.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
    basicauth {
        teamlead $2a$14$...   # caddy hash-password
    }
}
```
Caddy gets its own cert automatically; you only ever open 443 (and 80 for the
ACME challenge) in UFW — 8000 stays loopback-only, untouched.

**Your whole team, without exposing anything publicly:**
Put the VPS on a [Tailscale](https://tailscale.com/) (or similar) network and
change the binding to `ports: ["8000:8000"]` — reachable only over the private
mesh network, nothing public-facing at all.

If you ever bind the dashboard's port to `0.0.0.0` directly (skip the loopback
restriction) without one of the above in front of it, treat that as equivalent to
having no firewall on port 8000 — Docker will make it reachable regardless of any
`ufw deny 8000` you add. If you want UFW to actually be authoritative over
container ports, install [`ufw-docker`](https://github.com/chaifeng/ufw-docker)
instead of relying on `ufw deny`.

### Approve-endpoint auth is not a substitute for network exposure control

`DASHBOARD_APPROVE_TOKEN` gates the one mutating action (approving/un-approving a
week) with a shared secret, but the dashboard's *view* is unauthenticated by
design ("clients only need a browser," per the original ask). Don't rely on the
token alone if you expose the dashboard publicly — put real auth (the Caddy
`basicauth` above, or your proxy's equivalent) in front of it too.

### Outbound firewalling (optional hardening)

UFW's *default* outbound policy is `allow`, which is what both services need
(Telegram / Notion / AI over 443). If you tighten egress, allow at least DNS (53)
and HTTPS (443).
