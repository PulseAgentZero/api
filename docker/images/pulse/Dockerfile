# ─────────────────────────────────────────────────────────────────────────────
# Pulse — Self-Hosted All-in-One Image
#
# Bundles: Frontend (Next.js) · API · Worker · Agent · Scheduler · Redis · nginx
# Users only need a Postgres database — everything else is inside this image.
#
# Build (from the api/ directory):
#   docker buildx build \
#     -f docker/images/pulse/Dockerfile \
#     --build-context frontend=../dashboard \
#     -t pulseai/pulse:latest \
#     .
#
# Push to Docker Hub:
#   docker buildx build \
#     -f docker/images/pulse/Dockerfile \
#     --build-context frontend=../dashboard \
#     --platform linux/amd64,linux/arm64 \
#     --push \
#     -t pulseai/pulse:latest \
#     .
# ─────────────────────────────────────────────────────────────────────────────


# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM python:3.12-slim AS py-builder

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


# ── Stage 2: Next.js frontend build ──────────────────────────────────────────
# Uses the named build context: --build-context frontend=../dashboard
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

# Install dependencies first (layer-cached unless package.json changes)
COPY --from=frontend package.json package-lock.json* pnpm-lock.yaml* ./
RUN \
  if [ -f pnpm-lock.yaml ]; then \
    corepack enable && pnpm install --frozen-lockfile; \
  else \
    npm ci; \
  fi

# Copy the rest of the source
COPY --from=frontend . .

# Build vars baked into the static output
ENV NEXT_BUILD_STANDALONE=1
ENV NEXT_PUBLIC_DEPLOYMENT_MODE=self_hosted
# Empty = relative URLs → nginx routes /api/* to FastAPI on the same host
ENV NEXT_PUBLIC_API_URL=""
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build


# ── Stage 3: Runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Pulse (Self-Hosted)"
LABEL org.opencontainers.image.vendor="AgentZero"
LABEL org.opencontainers.image.description="All-in-one self-hosted Pulse image. Requires only a Postgres database."

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    DEPLOYMENT_MODE=self_hosted \
    # Tell the backend to allow requests from localhost (nginx in the same container)
    FRONTEND_URL=http://localhost

WORKDIR /app

# System packages:
#   libpq5        — Postgres client runtime lib
#   nodejs        — runs the Next.js standalone server.js
#   nginx         — unified reverse proxy on port 80
#   redis-server  — bundled Redis (no separate container needed)
#   curl          — health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    nginx \
    redis-server \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data/redis /var/log/pulse \
    && chmod 777 /data/redis /var/log/pulse

# Python packages from builder
COPY --from=py-builder /install /usr/local

# Backend application code
COPY app/        ./app/
COPY alembic.ini .
COPY scripts/    ./scripts/

# Next.js standalone output
#   .next/standalone/ → the minimal Node.js server (server.js + compiled app)
#   .next/static/     → hashed static assets (JS, CSS) — served via nginx alias
#   public/           → static public files
COPY --from=frontend-builder /frontend/.next/standalone/   /frontend/
COPY --from=frontend-builder /frontend/.next/static/       /frontend/.next/static/
COPY --from=frontend-builder /frontend/public/             /frontend/public/

# nginx config — replace the default site with ours
RUN rm -f /etc/nginx/sites-enabled/default
COPY docker/images/pulse/nginx.conf /etc/nginx/sites-enabled/pulse
RUN nginx -t

# supervisord config
COPY docker/images/pulse/supervisord.conf /etc/supervisor/conf.d/pulse.conf

# Entrypoint: Redis → wait for Postgres → migrations → supervisord (API + Next.js + nginx)
COPY docker/images/pulse/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Port 80: nginx (public — both frontend and API through one port)
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost/health || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
