# ─────────────────────────────────────────────────────────────────────────────
# Pulse — Self-Hosted All-in-One Image
#
# Bundles: API · Worker · Agent · Scheduler · Redis
# Users only need a Postgres database — everything else is inside this image.
#
# Build (from repo root):
#   docker build -f docker/images/pulse/Dockerfile -t pulseai/pulse:latest .
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
    httptools \
    supervisor


# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Pulse (Self-Hosted)"
LABEL org.opencontainers.image.vendor="AgentZero"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    DEPLOYMENT_MODE=self_hosted

WORKDIR /app

# System packages:
#   libpq5       — postgres client lib (runtime)
#   redis-server — bundled Redis (self-hosted doesn't need a separate Redis container)
#   curl         — health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    redis-server \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data/redis /var/log/pulse \
    && chmod 777 /data/redis /var/log/pulse

# Python packages from builder stage
COPY --from=builder /install /usr/local

# Application code
COPY app/        ./app/
COPY alembic.ini .
COPY scripts/    ./scripts/

# supervisord config — manages all processes inside the container
COPY docker/images/pulse/supervisord.conf /etc/supervisor/conf.d/pulse.conf

# Entrypoint: starts Redis, waits for Postgres, runs migrations, starts supervisord
COPY docker/images/pulse/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
