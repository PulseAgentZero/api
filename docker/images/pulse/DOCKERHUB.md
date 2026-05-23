# Entivia — Self-Hosted

**Real-time behavioral intelligence for any business.** Connect Entivia to your existing database and an autonomous AI agent pipeline scores your entities (customers, accounts, devices, anything), surfaces churn / fraud / opportunity signals, and generates next-best-action recommendations — all queryable through a conversational dashboard.

Entivia never copies your data. It introspects your schema, runs live SQL against your DB, and stores only the derived insights.

- Website: https://entivia.online
- Docs & License purchase: https://entivia.online
- Image tags: `latest`, `1.x.x` (semver per release)

---

## What's in the image

This is an **all-in-one image** (n8n-style). One container runs every Entivia service behind a single port:

| Component | Role |
|---|---|
| **Next.js dashboard** | Web UI |
| **FastAPI** | REST + public API |
| **Agent worker** | Conversational + tool-calling AI |
| **Pipeline worker** | Schema introspection · profiling · risk scoring · recommendations |
| **Scheduler** | Cron jobs (pipeline runs, billing, memory prune, usage reset) |
| **Redis** | Token rotation, rate limits, queues (bundled — no separate container) |
| **nginx** | Reverse proxy — exposes everything on port `80` |

You bring two things alongside it:

- **Postgres 16+** — Entivia's own metadata DB
- **Qdrant** — vector search for entity / RAG queries

---

## Quick start

Create a folder, drop in the two files below, then `docker compose up -d`.

### `docker-compose.yml`

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: entivia
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?missing — set POSTGRES_PASSWORD in .env}
      POSTGRES_DB: entivia
    volumes:
      - db_data:/var/lib/postgresql/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U entivia -d entivia"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

  qdrant:
    image: qdrant/qdrant:latest
    volumes:
      - qdrant_data:/qdrant/storage
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "bash -c '</dev/tcp/127.0.0.1/6333'"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

  entivia:
    image: chideraozigbo488/entivia:${ENTIVIA_VERSION:-latest}
    env_file:
      - path: .env
        required: false
    ports:
      - "${PORT:-80}:80"
    environment:
      DATABASE_URL: postgresql+asyncpg://entivia:${POSTGRES_PASSWORD}@db:5432/entivia
      DATABASE_SSLMODE: disable
      QDRANT_URL: http://qdrant:6333
      REDIS_URL: redis://127.0.0.1:6379/0
      LOCAL_STORAGE_PATH: /app/uploads
      HOME: /home/entivia
      FRONTEND_URL: ${FRONTEND_URL:-http://localhost}
    volumes:
      - entivia_data:/data
      - uploads_data:/app/uploads
      - entivia_logs:/var/log/entivia/streams
    depends_on:
      db:
        condition: service_healthy
      qdrant:
        condition: service_healthy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s

volumes:
  db_data:
  qdrant_data:
  entivia_data:
  entivia_logs:
  uploads_data:
```

### `.env`

```env
# Host
PORT=80
FRONTEND_URL=http://localhost

# Postgres (required)
POSTGRES_PASSWORD=change-me-to-a-long-random-string

# Security (required)
# Generate: openssl rand -hex 32
JWT_SECRET=replace-with-openssl-rand-hex-32

# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=replace-with-fernet-key

# LLM provider (at least one required)
ANTHROPIC_API_KEY=
GROQ_API_KEY=

# Embeddings (required for semantic entity search / RAG)
VOYAGEAI_API_KEY=

# Email (optional — verify / reset flows)
RESEND_API_KEY=
DEFAULT_FROM_EMAIL=noreply@example.com

# Google OAuth (optional)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Pro license (optional — paste in dashboard after install, or pre-provision here)
# PULSE_LICENSE_KEY=plc_…
```

### Run it

```bash
docker compose pull
docker compose up -d
```

Open **http://localhost** (or `http://<server-ip>`), create your first admin user, and connect your data source.

Health check:

```bash
curl -f http://localhost/health
```

---

## Generate secrets

```bash
# JWT_SECRET
openssl rand -hex 32

# ENCRYPTION_KEY (Fernet)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# POSTGRES_PASSWORD
openssl rand -base64 24 | tr -d '+/='
```

---

## Configuration reference

Compose wires `DATABASE_URL`, `QDRANT_URL`, and `REDIS_URL` automatically — don't override them in `.env` unless you're pointing at external services.

| Variable | Required | Description |
|---|:---:|---|
| `POSTGRES_PASSWORD` | yes | Password for the bundled Postgres |
| `JWT_SECRET` | yes | Token signing — `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | yes | Fernet key, encrypts client DB credentials at rest |
| `ANTHROPIC_API_KEY` | yes\* | Primary LLM (Claude) |
| `GROQ_API_KEY` | yes\* | Fallback LLM (Groq) |
| `VOYAGEAI_API_KEY` | yes | Embeddings for semantic search |
| `PORT` | no | Host port, default `80` |
| `FRONTEND_URL` | no | Public origin (CORS + asset URLs), default `http://localhost` |
| `PULSE_LICENSE_KEY` | no | Pre-provision a Pro license at boot (otherwise paste in dashboard) |
| `RESEND_API_KEY` | no | Resend, for verify / reset emails |
| `DEFAULT_FROM_EMAIL` | no | From address used with Resend |
| `GOOGLE_CLIENT_ID` / `_SECRET` | no | Google OAuth sign-in |
| `ENVIRONMENT` | no | `production` (default) or `development` |
| `LOG_LEVEL` | no | `INFO`, `DEBUG`, etc. |
| `PIPELINE_INTERVAL_HOURS` | no | Pipeline cron cadence, default `4` |
| `API_WORKERS` | no | uvicorn workers, default `1` |
| `AGENT_WORKERS` | no | Agent workers, default `1` |
| `REDIS_MAX_MEMORY` | no | Bundled Redis cap, default `256mb` |

\* At least one of `ANTHROPIC_API_KEY` or `GROQ_API_KEY` must be set. Both is recommended — Entivia automatically falls back if the primary provider errors.

---

## Activating Pro

The image runs in **Community** mode by default — everything works locally. Pro features (unlimited connections, advanced agents, SSO, audit log) are unlocked with a license key from https://entivia.online.

After purchase you receive a `plc_…` key. Two ways to apply it:

1. **Dashboard (recommended)** — sign in as an admin → Settings → License → paste the key. The instance activates online against `license.entivia.online` and persists locally.
2. **Pre-provision** — set `PULSE_LICENSE_KEY=plc_…` in `.env`. On first admin visit to Settings → License the instance auto-activates and the key shows as "Provisioned via .env". Ideal for IaC / CI deployments.

The license check runs once per day and degrades to a 14-day grace window if `license.entivia.online` is unreachable.

---

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Migrations run automatically on container start. Your volumes (`db_data`, `qdrant_data`, `entivia_data`, `uploads_data`) persist across upgrades.

We recommend pinning to a specific version in production:

```env
ENTIVIA_VERSION=1.0.0
```

---

## Custom port

To run behind your own reverse proxy or share a host:

```env
PORT=3000
```

Maps host `3000` → container `80`. Don't forget to update `FRONTEND_URL` if your public URL changes.

---

## Custom domain + HTTPS

Front Entivia with Caddy, Traefik, or nginx for TLS. Example with Caddy:

```caddy
entivia.example.com {
  reverse_proxy localhost:80
}
```

Then in `.env`:

```env
FRONTEND_URL=https://entivia.example.com
```

---

## Logs

```bash
docker compose logs -f entivia       # app processes (API, agent, pipeline, scheduler)
docker compose logs -f db            # Postgres
docker compose logs -f qdrant        # Vector DB
```

Inside the container, individual process streams live in `/var/log/entivia/streams/`.

---

## Backups

The bits worth backing up:

- `db_data` volume — all org data, users, recommendations, pipeline runs
- `qdrant_data` volume — embeddings (can be rebuilt, but rebuilding costs API calls)
- `uploads_data` volume — uploaded files
- Your `.env` — secrets

Postgres dump example:

```bash
docker compose exec db pg_dump -U entivia entivia | gzip > entivia-$(date +%F).sql.gz
```

---

## Troubleshooting

**`/health` returns 502 for the first ~90s.** Normal. The container runs migrations and warms the agent on first start. The healthcheck has a 90s `start_period`.

**Pipeline shows "Scheduler not seen recently".** The scheduler subprocess exited. Check `docker compose logs entivia | grep scheduler`. Most often: missing or invalid LLM key, or DB connectivity flaked during startup. Restart with `docker compose restart entivia`.

**`License activation failed`.** The instance needs outbound HTTPS to `license.entivia.online`. Check your firewall / egress proxy. The license also reverts to grace mode automatically if the server is briefly unreachable.

**Postgres won't start with `password authentication failed`.** You changed `POSTGRES_PASSWORD` after the volume was initialized. Either revert the password, or wipe the volume (`docker compose down -v` — destroys all data) and start fresh.

**Out of memory on small VPS.** Set `REDIS_MAX_MEMORY=128mb`, `API_WORKERS=1`, `AGENT_WORKERS=1`. Minimum comfortable size is 2 vCPU / 4 GB RAM.

---

## Architecture at a glance

```
                ┌────────────────────────────────────────────────────┐
                │  chideraozigbo488/entivia  (single container)      │
                │                                                    │
   :80 ──nginx──┼─► Next.js dashboard                                │
                │  └─► FastAPI ──► Agent worker                      │
                │             ├──► Pipeline worker (cron)            │
                │             └──► Scheduler                         │
                │                                                    │
                │  Redis (bundled, internal)                         │
                └──────────────┬─────────────────────────────────────┘
                               │
                ┌──────────────┴──────────────┐
                ▼                             ▼
        ┌──────────────┐              ┌──────────────┐
        │ Postgres 16  │              │   Qdrant     │
        │  (your data) │              │ (embeddings) │
        └──────────────┘              └──────────────┘
                ▲
                │ live SQL (never copied)
                │
        ┌───────┴────────┐
        │ Your business  │
        │   database     │
        │ (Postgres,     │
        │  MySQL, MSSQL, │
        │  SQLite…)      │
        └────────────────┘
```

The agent pipeline is four stages, each an autonomous LLM-backed agent:

1. **Schema Intelligence** — introspects and annotates your DB schema
2. **Profiling** — builds behavioral profiles by joining across your tables
3. **Risk Scoring** — deterministic tiering + LLM-generated narratives
4. **Recommendations** — structured next-best-actions you can route to ops or CRM

---

## Supported architectures

`linux/amd64`, `linux/arm64` (Apple Silicon, AWS Graviton, Ampere).

---

## Image labels

```
org.opencontainers.image.title=Entivia (Self-Hosted)
org.opencontainers.image.vendor=AgentZero
org.opencontainers.image.description=All-in-one self-hosted Entivia image.
                                     Requires Postgres and Qdrant; bundles Redis
                                     and all app services.
```

---

## Support

- Product & pricing: https://entivia.online
- Email: support@entivia.online
- License issues: license@entivia.online

If you hit a bug, include `docker compose logs entivia --tail 200` and the output of `curl -s http://localhost/health` in your report.

---

## License

The Entivia software is proprietary. Community usage is free for evaluation and small teams; Pro features and production deployments require a license key from https://entivia.online. See the EULA shown in the dashboard on first launch.
