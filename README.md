# Pulse — Backend API

Real-Time Intelligence for Any Business.

---

## Local Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in env vars
cp .env.example .env

# 3. Run migrations
alembic upgrade head

# 4. Start with hot reload
uvicorn app.api.app:app --reload --host 0.0.0.0 --port 8000
```

---

## Docker (recommended)

Runs the full stack locally — api, worker, agent, scheduler, redis, and postgres — matching what runs in production.

```bash
# Copy env and fill in values
cp docker/compose/cloud/.env.example docker/compose/cloud/.env

# Build and start
docker compose -f docker/compose/cloud/docker-compose.yml up --build

# Detached
docker compose -f docker/compose/cloud/docker-compose.yml up -d
```

> To build from source instead of pulling the image, uncomment the `build:` block in `docker/compose/cloud/docker-compose.yml`.

**Common commands:**

```bash
# Logs
docker compose -f docker/compose/cloud/docker-compose.yml logs -f
docker compose -f docker/compose/cloud/docker-compose.yml logs -f api

# Restart a service after code changes
docker compose -f docker/compose/cloud/docker-compose.yml restart api

# Rebuild after dependency changes
docker compose -f docker/compose/cloud/docker-compose.yml up --build api

# Stop
docker compose -f docker/compose/cloud/docker-compose.yml down

# Full reset (wipes volumes)
docker compose -f docker/compose/cloud/docker-compose.yml down -v
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | Postgres — `postgresql+asyncpg://user:pass@host:5432/db` |
| `JWT_SECRET` | Yes | `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | Yes | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `GROQ_API_KEY` | Yes | Groq fallback LLM key |
| `REDIS_URL` | Yes | `redis://localhost:6379/0` |
| `RESEND_API_KEY` | No | Email (verify/reset flows) |
| `GOOGLE_CLIENT_ID` | No | Google OAuth |
| `GOOGLE_CLIENT_SECRET` | No | Google OAuth |
| `VOYAGEAI_API_KEY` | No | Semantic entity search |
| `QDRANT_URL` | No | Vector database |
| `FRONTEND_URL` | No | CORS origin — default `http://localhost:3000` |

---

## Migrations

```bash
alembic upgrade head                              # apply all
alembic revision --autogenerate -m "description" # new migration
alembic downgrade -1                              # roll back one
```

---

## API Docs

| URL | What |
|---|---|
| `http://localhost:8000/docs` | Swagger UI — interactive (dev only) |
| `http://localhost:8000/redoc` | ReDoc — shareable, always on |
| `http://localhost:8000/openapi.json` | Import into Postman / Scalar |
| `http://localhost:8000/api/public/redoc` | Public API docs |

---

## Project Structure

```
app/
├── api/
│   ├── app.py              # FastAPI app — routers, middleware, error handlers
│   ├── auth/               # JWT, login, signup, OAuth
│   ├── routes/             # Internal API  (/api/v1/*)
│   ├── public/             # Public API    (/api/public/v1/*)
│   ├── dependencies/       # Auth, plan gate dependencies
│   └── errors.py           # Shared error helpers
│
├── agents/                 # AI pipeline — owned by AI engineer
│
├── services/               # Business logic, schedulers
│
├── infrastructure/
│   ├── database/
│   │   ├── models/         # ORM models
│   │   ├── repositories/   # Data access layer
│   │   └── alembic/        # Migrations
│   ├── redis/              # Client, token helpers
│   ├── crypto.py           # Fernet encryption
│   └── email/              # Resend integration
│
└── config/
    └── settings.py         # All env var config

docker/
├── images/pulse-cloud/     # Cloud image (PULSE_SERVICE selects mode)
└── compose/cloud/          # Dev / internal compose

scripts/db/
├── seed_telecom_db.py      # Demo data
└── reset_db.py             # Wipe DB
```

---

## Health Check

```bash
curl http://localhost:8000/health
```

---

## Reference Docs

| File | What |
|---|---|
| `BACKEND_ROUTES.md` | Full endpoint reference |
| `SCHEMA.md` | Database schema (all 25 tables) |
| `MILESTONES.md` | Feature roadmap |
| `LICENSE_SYSTEM.md` | License key flow |
