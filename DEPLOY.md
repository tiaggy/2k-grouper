# Deploying the capture bot on a VPS

The bot is a single Docker container, **long-polling / outbound-only** (no inbound
ports). That makes VPS deployment simple and the attack surface tiny. Below is a
concrete, copy-pasteable plan using **Ubuntu 24.04 LTS**; any small Linux VPS works.

---

## 0. Before you start — ROTATE the secrets ⚠️

Every secret was pasted into a chat during development, so treat them as burned.
**Rotate all of them and only ever put the fresh values in the server's `.env`:**

- **Telegram bot token** — BotFather → `/token` (or `/revoke`) for @group_2k_bot.
- **Notion integration token** — Notion → Settings → Connections → your integration → refresh secret.
- **OpenAI / proxy key** — reissue from the proxy dashboard.

Never commit `.env`; never paste the new values anywhere but the server.

---

## 1. Pick the VPS

The workload is minimal (idle most of the time, a little AI/HTTP traffic).

- **Size:** 1 vCPU, 1–2 GB RAM, 20 GB SSD is plenty. (e.g. Hetzner **CX22**, ~€4/mo.)
- **OS:** Ubuntu 24.04 LTS.
- **Region:** anywhere — outbound only. EU (Nuremberg/Helsinki) is fine for Ukraine/Azerbaijan teams.
- Add your **SSH public key** during creation.

---

## 2. First-boot hardening (as root, then a sudo user)

```bash
ssh root@SERVER_IP

# Non-root sudo user
adduser deploy && usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy   # copy your key

# Updates + basics
apt update && apt -y upgrade
apt -y install ufw fail2ban unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades   # enable auto security updates
```

Harden SSH (`/etc/ssh/sshd_config`): `PermitRootLogin no`, `PasswordAuthentication no`,
then `systemctl restart ssh`. Re-login as `deploy` and confirm sudo works **before**
closing the root session.

---

## 3. Firewall (UFW) — SSH only

The bot needs **no inbound port**, so allow only SSH:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing     # bot needs outbound 443 (Telegram/Notion/AI)
sudo ufw allow OpenSSH
sudo ufw enable
```

> **Docker + UFW note:** irrelevant here because we publish **no ports** — Docker
> only bypasses UFW for *published* ports, and there are none. See `README.docker.md`.
> If you ever add an inbound port, read that file first.

---

## 4. Install Docker Engine + Compose plugin

```bash
# Official repo
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update && sudo apt -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin

sudo usermod -aG docker deploy      # run docker without sudo (re-login to apply)
sudo systemctl enable --now docker  # start on boot
```

---

## 5. Get the code onto the server

**Option A — private Git repo (recommended):**
```bash
cd ~ && git clone git@github.com:YOU/telegram-bot.git && cd telegram-bot
```
**Option B — copy from your machine:**
```bash
# from your Windows box (Git Bash), excluding secrets/state:
scp -r ./telegram-bot deploy@SERVER_IP:~/telegram-bot
```

---

## 6. Create `.env` on the server (secrets live only here)

```bash
cd ~/telegram-bot
nano .env       # paste the ROTATED values
chmod 600 .env  # owner-only
```

Required keys (fill with the **new** secrets):
```
TELEGRAM_BOT_TOKEN=...
OWNER_USER_ID=2032344632
NOTION_TOKEN=...
NOTION_DB_ID=39df38df-3bd8-8063-9418-d009823ddb7f
NOTION_DB_DS_ID=6eff38df-3bd8-83a8-b59a-8787f1a0016e
NOTION_CONFIG_DB_ID=39af38df-3bd8-8164-b189-e3e96d859682
NOTION_TRACKED_DB_ID=39af38df-3bd8-816d-93dc-ec967ba026d5
NOTION_ACCOUNTS_DB_ID=39af38df-3bd8-8179-bdbf-d23e0d7c3f81
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://cliproxyapi.tiaggy.space/v1
OPENAI_MODEL=claude-sonnet-4-6
OPENAI_FALLBACK_MODEL=gpt-5.4-mini   # used if the primary errors / is cooling down
AI_ASSIST=on
TZ=Asia/Baku        # your workers' timezone — drives start-of-day missing + weekends
```

> Set **`TZ`** to the timezone that defines a workday for your team (`Asia/Baku`,
> `Europe/Kyiv`, …). The 00:00 missing sweep and weekend skip use local time.

---

## 7. One-poller rule ⚠️

Telegram allows only **one** `getUpdates` poller per token. **Stop the local
container** (`docker compose down` on your Windows box) before starting the VPS one,
or both get 409 conflicts. The VPS resumes cleanly from the Notion `Last Msg ID`
markers even with a fresh volume.

---

## 8. Launch

```bash
docker compose up -d --build
docker compose logs -f          # watch startup; Ctrl-C to stop following
```

Expect: `tracked groups: [...]`, `notion: ON`, `resume: 4 group(s)`, no `degraded`.
Then **send a `+` in a tracked group** and confirm it's captured.

---

## 9. Operations

| Task | Command |
|---|---|
| Follow logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Update after code change | `git pull && docker compose up -d --build` |
| Stop (state persists) | `docker compose down` |
| Shell into container | `docker compose exec bot sh` |
| Inspect state volume | `docker run --rm -v telegram-bot_bot-state:/d busybox ls -la /d` |

- **Auto-restart & boot:** `restart: unless-stopped` + `systemctl enable docker`
  already cover crashes and reboots. The owner **Restart** button works
  (`EXIT_ON_RESTART=1` → Compose relaunches).
- **Logs** are rotated (10 MB × 3) by the compose `logging` config.
- **Alerts:** the bot DMs the owner on Notion/AI/Telegram outages — no extra
  monitoring strictly needed, but an uptime pinger on the VPS (e.g. Uptime Kuma /
  a Healthchecks.io cron) is a nice add.

---

## 10. Backups (low priority)

Notion is the source of truth (records + resume markers), so a lost volume only
costs the local `events.jsonl` audit log and in-flight spool. If you want them:

```bash
# nightly cron: snapshot the volume
docker run --rm -v telegram-bot_bot-state:/d -v /home/deploy/backups:/b busybox \
  tar czf /b/bot-state-$(date +\%F).tgz -C /d .
```

---

## Security checklist

- [ ] All secrets **rotated** after development; only fresh values in server `.env`.
- [ ] `.env` is `chmod 600`, never committed.
- [ ] SSH: key-only, root login disabled, `fail2ban` on.
- [ ] UFW: deny incoming except SSH; **no container ports published**.
- [ ] Container runs as **non-root** (`botuser`, already in the image).
- [ ] Unattended security upgrades enabled; `docker` + base image updated periodically.
- [ ] Only **one** poller runs against the token at a time.
