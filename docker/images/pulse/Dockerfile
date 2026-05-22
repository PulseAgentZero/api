# ─────────────────────────────────────────────────────────────────────────────
# Entivia — Self-Hosted All-in-One Image
#
# All-in-one self-hosted image (n8n-style): Frontend · API · Worker · Agent · Scheduler · Redis · nginx
# End users pull chideraozigbo488/entivia and run docker/compose/self-hosted (Postgres + Qdrant only).
# Requires Postgres + Qdrant alongside this container; Redis and all app processes are bundled here.
#
# Set VERSION when releasing — both `:VERSION` and `:latest` are tagged.
#
# Local single-arch build (fastest — your Mac arch only, ~5–10 min cold, ~1 min warm):
#
#   export VERSION=1.0.0
#   docker buildx build \
#     -f docker/images/pulse/Dockerfile \
#     --build-context frontend=/Users/ozigbochidera/Desktop/dashboard \
#     --build-arg PULSE_LICENSE_SERVER_URL=https://license.entivia.online \
#     --build-arg PULSE_LICENSE_JWT_ISSUER=https://license.entivia.online \
#     --build-arg PULSE_LICENSE_PUBLIC_KEY="$(cat license_public.pem)" \
#     -t chideraozigbo488/entivia:$VERSION \
#     -t chideraozigbo488/entivia:latest \
#     --load \
#     .
#
# Multi-arch publish (~2x slower — only for releases pushed to Docker Hub):
#
#   export VERSION=1.0.0
#   docker login
#   docker buildx build \
#     -f docker/images/pulse/Dockerfile \
#     --build-context frontend=/Users/ozigbochidera/Desktop/dashboard \
#     --platform linux/amd64,linux/arm64 \
#     --build-arg PULSE_LICENSE_SERVER_URL=https://license.entivia.online \
#     --build-arg PULSE_LICENSE_JWT_ISSUER=https://license.entivia.online \
#     --build-arg PULSE_LICENSE_PUBLIC_KEY="$(cat license_public.pem)" \
#     -t chideraozigbo488/entivia:$VERSION \
#     -t chideraozigbo488/entivia:latest \
#     --push \
#     .
#
# Speed tips:
#   - Buildx already enables BuildKit; this Dockerfile uses --mount=type=cache so
#     re-builds skip wheel + npm downloads entirely.
#   - On a slow link, build single-arch first; multi-arch only when publishing.
#   - To prune build cache later: `docker buildx prune --filter type=exec.cachemount`.
# ─────────────────────────────────────────────────────────────────────────────


# ── Stage 1: Python dependencies ─────────────────────────────────────────────
# Uses BuildKit cache mounts so apt + pip artifacts persist across builds.
# Requires DOCKER_BUILDKIT=1 (default with `docker buildx build`).
FROM python:3.12-slim AS py-builder

WORKDIR /build

# Build-time system libs:
#   gcc, libpq-dev    — for any sdist that still needs to compile
#   pkg-config, lxml/xmlsec headers — for python3-saml's xmlsec dep on older arches
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && apt-get install -y --no-install-recommends \
      gcc \
      libpq-dev \
      pkg-config \
      libxml2-dev \
      libxmlsec1-dev \
      libxmlsec1-openssl

COPY requirements.txt .

# pip:
#   --mount=type=cache,target=/root/.cache/pip → reuse downloaded wheels across builds
#   --timeout=180 --retries=10 → survive slow / flaky PyPI links (35MB scipy etc.)
#   --prefer-binary → never compile from sdist if a wheel exists
ENV PIP_DEFAULT_TIMEOUT=180 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_COMPILE=1
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --prefix=/install --prefer-binary --retries=10 \
        -r requirements.txt \
        uvloop \
        httptools \
        supervisor \
 && find /install -depth -type d -name "__pycache__" -exec rm -rf {} + \
 && find /install -depth -type d -name "tests" -exec rm -rf {} + \
 && find /install -depth -type d -name "test" -exec rm -rf {} + \
 && find /install -name "*.pyc" -delete \
 && find /install -name "*.pyo" -delete


# ── Stage 2: Next.js frontend build ──────────────────────────────────────────
# Uses the named build context: --build-context frontend=../dashboard
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

# Install dependencies first (layer-cached unless package.json changes)
COPY --from=frontend package.json package-lock.json* pnpm-lock.yaml* ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    --mount=type=cache,target=/frontend/.npm-cache,sharing=locked \
    if [ -f pnpm-lock.yaml ]; then \
      corepack enable && pnpm install --frozen-lockfile; \
    else \
      npm ci --prefer-offline --no-audit --no-fund; \
    fi

# Copy the rest of the source
COPY --from=frontend . .

# Build vars baked into the static output (override only via --build-arg)
ARG PULSE_DEPLOYMENT_MODE=self_hosted
ARG NEXT_PUBLIC_MARKETING_URL=https://entivia.online
ENV NEXT_BUILD_STANDALONE=1
ENV NEXT_PUBLIC_DEPLOYMENT_MODE=${PULSE_DEPLOYMENT_MODE}
# Empty = relative URLs → nginx routes /api/* to FastAPI on the same host
ENV NEXT_PUBLIC_API_URL=""
# Marketing site self-hosted users get redirected to for license purchase / pricing
ENV NEXT_PUBLIC_MARKETING_URL=${NEXT_PUBLIC_MARKETING_URL}
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build


# ── Stage 3: Runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim

# License + deployment mode: set only at image build (not via customer .env at runtime).
ARG PULSE_LICENSE_SERVER_URL=https://license.entivia.online
ARG PULSE_LICENSE_JWT_ISSUER=https://license.entivia.online
ARG PULSE_LICENSE_PUBLIC_KEY=

LABEL org.opencontainers.image.title="Entivia (Self-Hosted)"
LABEL org.opencontainers.image.vendor="AgentZero"
LABEL org.opencontainers.image.description="All-in-one self-hosted Entivia image. Requires Postgres and Qdrant; bundles Redis and all app services."

# Default FRONTEND_URL for first-run; override via .env / docker-compose for real deployments.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    HOME=/home/entivia \
    FRONTEND_URL=http://localhost

ENV PULSE_LICENSE_SERVER_URL=${PULSE_LICENSE_SERVER_URL} \
    PULSE_LICENSE_JWT_ISSUER=${PULSE_LICENSE_JWT_ISSUER} \
    PULSE_LICENSE_PUBLIC_KEY=${PULSE_LICENSE_PUBLIC_KEY}

ARG PULSE_MARKETING_URL=https://entivia.online
ENV PULSE_MARKETING_URL=${PULSE_MARKETING_URL}

RUN mkdir -p /etc/pulse && python3 -c "\
import json, os; \
cfg = { \
    'deployment_mode': 'self_hosted', \
    'license_server_url': os.environ.get('PULSE_LICENSE_SERVER_URL', '').rstrip('/'), \
    'license_jwt_issuer': os.environ.get('PULSE_LICENSE_JWT_ISSUER', '').rstrip('/') or None, \
    'marketing_url': os.environ.get('PULSE_MARKETING_URL', '').rstrip('/') or None, \
}; \
pk = (os.environ.get('PULSE_LICENSE_PUBLIC_KEY') or '').strip(); \
cfg.update({'license_public_key': pk} if pk else {}); \
open('/etc/pulse/build-config.json', 'w').write(json.dumps(cfg)) \
"

WORKDIR /app

# System packages:
#   libpq5             — Postgres client runtime lib
#   libxmlsec1*        — required by python3-saml (SSO/SAML)
#   nodejs             — runs the Next.js standalone server.js
#   nginx              — unified reverse proxy on port 80
#   redis-server       — bundled Redis (no separate container needed)
#   curl               — health checks
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && apt-get install -y --no-install-recommends \
      libpq5 \
      libxml2 \
      libxmlsec1 \
      libxmlsec1-openssl \
      nginx \
      redis-server \
      curl \
      gosu \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /usr/lib/node_modules/npm \
    && rm -f /usr/bin/npm /usr/bin/npx /usr/bin/corepack \
    && useradd -m -u 1001 -s /bin/sh entivia \
    && mkdir -p /data/redis /var/log/entivia /var/log/entivia/streams /app/uploads \
    && chown -R entivia:entivia /data/redis /var/log/entivia /app/uploads \
    && chmod 775 /data/redis /var/log/entivia /var/log/entivia/streams

# Python packages from builder
COPY --from=py-builder /install /usr/local

# Backend application code
COPY app/        ./app/
COPY alembic.ini .
COPY scripts/    ./scripts/
RUN chown -R entivia:entivia /app

# Next.js standalone output
#   .next/standalone/ → the minimal Node.js server (server.js + compiled app)
#   .next/static/     → hashed static assets (JS, CSS) — served via nginx alias
#   public/           → static public files
COPY --from=frontend-builder /frontend/.next/standalone/   /frontend/
COPY --from=frontend-builder /frontend/.next/static/       /frontend/.next/static/
COPY --from=frontend-builder /frontend/public/             /frontend/public/
RUN chown -R entivia:entivia /frontend

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
