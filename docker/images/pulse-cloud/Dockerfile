# ─────────────────────────────────────────────────────────────────────────────
# Pulse — Cloud / Internal Image
#
# Lean single-service image. PULSE_SERVICE env var selects what runs:
#   api        REST API (auth, connections, recommendations…)
#   worker     Pipeline execution (pulls jobs from Redis queue)
#   agent      Conversational AI + SSE streaming
#   scheduler  Cron trigger (pushes jobs to queue on schedule)
#
# No bundled Redis — each service connects to a shared Redis instance.
# Scale independently:  docker compose up --scale worker=4
#
# Build (from repo root):
#   docker build -f docker/images/pulse-cloud/Dockerfile -t pulseai/pulse-cloud:latest .
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: install Python dependencies ─────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install \
    -r requirements.txt \
    uvloop \
    httptools


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Pulse Cloud"
LABEL org.opencontainers.image.vendor="AgentZero"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    DEPLOYMENT_MODE=cloud \
    PULSE_SERVICE=api

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1001 -s /bin/sh pulse \
    && chown -R pulse:pulse /app \
    && mkdir -p /app/uploads \
    && chown pulse:pulse /app/uploads

COPY --from=builder /install /usr/local

COPY --chown=pulse:pulse app/        ./app/
COPY --chown=pulse:pulse alembic.ini .
COPY --chown=pulse:pulse scripts/    ./scripts/

COPY docker/images/pulse-cloud/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Entrypoint starts as root to fix volume permissions, then drops to `pulse` via gosu.
EXPOSE 8000

# Per-service healthchecks are defined in docker-compose (api/agent use HTTP;
# worker/scheduler use scripts/docker/redis_healthcheck.py).
HEALTHCHECK NONE

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["api"]
