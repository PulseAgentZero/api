#!/bin/sh
set -e

# ─────────────────────────────────────────────────────────────────────────────
# Entivia self-hosted entrypoint
# Order: start Redis → wait for Postgres → run migrations → start supervisord
# supervisord manages: API · Worker · Agent · Scheduler · Next.js · nginx
# ─────────────────────────────────────────────────────────────────────────────

export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
API_WORKERS="${API_WORKERS:-1}"
AGENT_WORKERS="${AGENT_WORKERS:-1}"

echo "[entivia] REDIS_URL=${REDIS_URL}"
echo "[entivia] QDRANT_URL=${QDRANT_URL:-<not set>}"

# ── 0. Writable storage for the `entivia` app user (uid 1001) ─────────────────
# Named volumes (e.g. uploads_data:/app/uploads) are often created root-owned.
storage="${LOCAL_STORAGE_PATH:-/app/uploads}"
mkdir -p "$storage"
chown -R entivia:entivia "$storage"
chown -R entivia:entivia /data/redis
chown -R entivia:entivia /var/log/entivia

# ── 1. Remove the nginx default site so ours is the only one ─────────────────
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

# ── 2. Start bundled Redis ────────────────────────────────────────────────────
echo "[entivia] starting bundled Redis..."
gosu entivia redis-server \
  --daemonize yes \
  --dir /data/redis \
  --maxmemory "${REDIS_MAX_MEMORY:-256mb}" \
  --maxmemory-policy allkeys-lru \
  --appendonly yes \
  --appendfsync everysec \
  --logfile /var/log/entivia/redis.log

# Wait for Redis to accept connections
for i in $(seq 1 10); do
  redis-cli ping > /dev/null 2>&1 && break || sleep 1
done
echo "[entivia] Redis ready"

# ── 3. Wait for Postgres ──────────────────────────────────────────────────────
if [ -n "$DATABASE_URL" ]; then
  echo "[entivia] waiting for database..."
  python - <<'EOF'
import asyncio, sys, os, time

async def wait():
    try:
        import asyncpg
    except ImportError:
        print("[entivia] asyncpg not available, skipping DB wait")
        return

    url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    for i in range(30):
        try:
            conn = await asyncpg.connect(url)
            await conn.close()
            print("[entivia] database ready")
            return
        except Exception as e:
            if i == 0:
                print(f"[entivia] database not ready, retrying ({e})")
            await asyncio.sleep(2)
    print("[entivia] ERROR: database never became available")
    sys.exit(1)

asyncio.run(wait())
EOF
fi

# ── 4. Run database migrations ────────────────────────────────────────────────
echo "[entivia] running migrations..."
gosu entivia alembic upgrade head
echo "[entivia] migrations done"

# ── 5. Export env vars for supervisord programs ───────────────────────────────
export API_WORKERS
export AGENT_WORKERS

# ── 6. Hand off to supervisord ────────────────────────────────────────────────
echo "[entivia] starting all services via supervisord..."
exec supervisord -c /etc/supervisor/conf.d/pulse.conf
