#!/bin/sh
set -e

# ─────────────────────────────────────────────────────────────────────────────
# Pulse self-hosted entrypoint
# Order: start Redis → wait for Postgres → run migrations → start supervisord
# supervisord manages: API · Worker · Agent · Scheduler · Next.js · nginx
# ─────────────────────────────────────────────────────────────────────────────

REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
API_WORKERS="${API_WORKERS:-1}"
AGENT_WORKERS="${AGENT_WORKERS:-1}"

# ── 0. Remove the nginx default site so ours is the only one ─────────────────
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

# ── 1. Start bundled Redis ────────────────────────────────────────────────────
echo "[pulse] starting bundled Redis..."
redis-server \
  --daemonize yes \
  --dir /data/redis \
  --maxmemory "${REDIS_MAX_MEMORY:-256mb}" \
  --maxmemory-policy allkeys-lru \
  --appendonly yes \
  --appendfsync everysec \
  --logfile /var/log/pulse/redis.log

# Wait for Redis to accept connections
for i in $(seq 1 10); do
  redis-cli ping > /dev/null 2>&1 && break || sleep 1
done
echo "[pulse] Redis ready"

# ── 2. Wait for Postgres ──────────────────────────────────────────────────────
if [ -n "$DATABASE_URL" ]; then
  echo "[pulse] waiting for database..."
  python - <<'EOF'
import asyncio, sys, os, time

async def wait():
    try:
        import asyncpg
    except ImportError:
        print("[pulse] asyncpg not available, skipping DB wait")
        return

    url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    for i in range(30):
        try:
            conn = await asyncpg.connect(url)
            await conn.close()
            print("[pulse] database ready")
            return
        except Exception as e:
            if i == 0:
                print(f"[pulse] database not ready, retrying ({e})")
            await asyncio.sleep(2)
    print("[pulse] ERROR: database never became available")
    sys.exit(1)

asyncio.run(wait())
EOF
fi

# ── 3. Run database migrations ────────────────────────────────────────────────
echo "[pulse] running migrations..."
alembic upgrade head
echo "[pulse] migrations done"

# ── 4. Export env vars for supervisord programs ───────────────────────────────
export API_WORKERS
export AGENT_WORKERS

# ── 5. Hand off to supervisord ────────────────────────────────────────────────
echo "[pulse] starting all services via supervisord..."
exec supervisord -c /etc/supervisor/conf.d/pulse.conf
