# Telegram attendance-capture bot — long-polling, OUTBOUND-ONLY.
# It opens no listening sockets, so the image EXPOSEs nothing and the container
# publishes no ports (see the Docker + UFW note in README.docker.md).
FROM python:3.13-slim-bookworm

# tzdata: the missing-sweep hour + weekend logic use local time, so the container
# must know its timezone (set TZ in the environment). ca-certificates for HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_DIR=/data \
    EXIT_ON_RESTART=1

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only (state files live in /data, a mounted volume).
COPY *.py ./

# Persistent runtime state (progress, spool, caches, events log).
RUN mkdir -p /data && useradd -m -u 10001 botuser && chown -R botuser /app /data
USER botuser
VOLUME ["/data"]

# No HEALTHCHECK that hits Telegram (would burn API calls); the bot's own health
# gate + restart policy handle liveness.
CMD ["python", "bot.py"]
