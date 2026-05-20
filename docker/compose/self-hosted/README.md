# Pulse — Self-Hosted

Pull and run Pulse: one application container plus Postgres and Qdrant.

| Service | What it runs |
|---------|----------------|
| **pulse** | UI (Next.js), API, worker, agent, scheduler, Redis, nginx — single port **80** |
| **db** | Postgres 16 |
| **qdrant** | Vector database (entity search / RAG) |

Config: [`docker-compose.yml`](docker-compose.yml) · env template: [`.env.example`](.env.example)

## Quick start

```bash
cp .env.example .env
# Edit .env — at minimum set POSTGRES_PASSWORD, JWT_SECRET, ENCRYPTION_KEY,
# and ANTHROPIC_API_KEY or GROQ_API_KEY

docker compose pull
docker compose up -d
```

Open **http://localhost** (or `http://<your-server-ip>`).

Health check:

```bash
curl -f http://localhost/health
```

## Upgrade

```bash
docker compose pull
docker compose up -d
```

## Environment

Compose sets these for you — do not put them in `.env` unless you know why:

- `DATABASE_URL` → `db` service
- `QDRANT_URL` → `http://qdrant:6333`
- `REDIS_URL` → bundled Redis inside `pulse`

Deployment mode, license server URL, and license verification public key are baked into the `pulseai/pulse` image at build time. Activate Pro with your `plc_…` key in the dashboard after install.

## Custom port

In `.env`:

```env
PORT=3000
```

Maps host `3000` → nginx `80` inside the container.

## Logs and stop

```bash
docker compose logs -f pulse
docker compose down
```

From the API repo root you can also use: `make sh-up`, `make sh-logs`, `make sh-pull`.
