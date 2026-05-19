#!/bin/sh
set -e

# When the container starts as root, ensure mounted storage paths are writable by
# the `pulse` user (uid 1001). Named volumes are often created root-owned.
if [ "$(id -u)" = "0" ]; then
  storage="${LOCAL_STORAGE_PATH:-/app/uploads}"
  mkdir -p "$storage"
  chown -R pulse:pulse "$storage"
  if [ -d /app/logs ]; then
    chown -R pulse:pulse /app/logs
  fi
  exec gosu pulse "$0" "$@"
fi

SERVICE="${1:-${PULSE_SERVICE:-api}}"

echo "[pulse:$SERVICE] starting..."

# ── Run migrations (API service only — it goes first, others wait) ────────────
if [ "$SERVICE" = "api" ]; then
  echo "[pulse:api] running migrations..."
  alembic upgrade head
  echo "[pulse:api] migrations done"
fi

# ── Start the selected service ────────────────────────────────────────────────
case "$SERVICE" in

  api)
    exec uvicorn app.api.app:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --workers "${API_WORKERS:-2}" \
      --loop uvloop \
      --http httptools \
      --proxy-headers \
      --forwarded-allow-ips "*"
    ;;

  worker)
    exec python -m app.worker
    ;;

  agent)
    exec uvicorn app.conversational.app:app \
      --host 0.0.0.0 \
      --port "${AGENT_PORT:-8001}" \
      --workers "${AGENT_WORKERS:-1}" \
      --loop uvloop \
      --http httptools
    ;;

  scheduler)
    # Always run exactly ONE instance — uses a Redis distributed lock internally.
    exec python -m app.services.schedulers.pipeline_scheduler
    ;;

  *)
    echo "[pulse] unknown service '$SERVICE'"
    echo "[pulse] valid options: api | worker | agent | scheduler"
    exit 1
    ;;

esac
