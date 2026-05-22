# ─────────────────────────────────────────────────────────────────────────────
# Pulse — License Server Image
#
# Internal service that validates license keys for self-hosted customers.
# Not exposed publicly — only reachable inside the pulse network.
#
# Build (from repo root):
#   docker build -f docker/images/pulse-license/Dockerfile -t chideraozigbo488/entivia-license:latest .
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install \
    -r requirements.txt \
    uvloop \
    httptools


FROM python:3.12-slim

LABEL org.opencontainers.image.title="Pulse License Server"
LABEL org.opencontainers.image.vendor="AgentZero"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1001 -s /bin/sh entivia \
    && chown -R entivia:entivia /app

COPY --from=builder /install /usr/local

COPY --chown=entivia:entivia app/ ./app/
COPY --chown=entivia:entivia alembic.ini .

USER entivia

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

CMD ["uvicorn", "app.license_server.app:app", \
     "--host", "0.0.0.0", "--port", "8002", \
     "--loop", "uvloop", "--http", "httptools"]
