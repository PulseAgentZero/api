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

# 4. Start API with hot reload
uvicorn app.api.app:app --reload --host 0.0.0.0 --port 8000

# 5. (Separate terminal) Background crons — pipeline, billing, memory prune, etc.
make dev-scheduler
```

The API does not start schedulers. For scheduled pipeline runs, billing jobs, and similar crons, run `make dev-scheduler` alongside the API (same as cloud `scheduler` service / self-hosted supervisord).

---

## Docker — Self-hosted (production)

Pull and run — similar to n8n: one pre-built **pulse** image (UI, API, worker, agent, scheduler, Redis, nginx on port 80) plus **Postgres** and **Qdrant**.

```bash
cd docker/compose/self-hosted
cp .env.example .env   # fill POSTGRES_PASSWORD, JWT_SECRET, ENCRYPTION_KEY, LLM keys, PULSE_LICENSE_PUBLIC_KEY
docker compose pull
docker compose up -d
open http://localhost
```

From the repo root: `make sh-pull && make sh-up`, `make sh-logs`.

Maintainers publishing the image: `make build-self-hosted` (bundles the dashboard from `PULSE_DASHBOARD_DIR`, default in Makefile).

---

## Docker — Cloud / internal dev

Runs api, worker, agent, scheduler, redis, postgres, and qdrant as separate services — matching Pulse cloud infrastructure.

```bash
cp docker/compose/cloud/.env.example docker/compose/cloud/.env
docker compose -f docker/compose/cloud/docker-compose.yml up --build -d
```

> To build from source instead of pulling the image, uncomment the `build:` block in `docker/compose/cloud/docker-compose.yml`.

**Common commands:**

```bash
docker compose -f docker/compose/cloud/docker-compose.yml logs -f
docker compose -f docker/compose/cloud/docker-compose.yml restart api
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
| `REDIS_URL` | Cloud / local dev | `redis://localhost:6379/0` — bundled in self-hosted image (set by compose) |
| `RESEND_API_KEY` | No | Email (verify/reset flows) |
| `GOOGLE_CLIENT_ID` | No | Google OAuth |
| `GOOGLE_CLIENT_SECRET` | No | Google OAuth |
| `VOYAGEAI_API_KEY` | No | Semantic entity search |
| `QDRANT_URL` | Self-hosted compose | Set automatically to `http://qdrant:6333` — optional for local dev |
| `FRONTEND_URL` | No | CORS origin — default `http://localhost:3000` |
| `LICENSE_SERVER_URL` | Self-hosted image | Set at **image build** via `--build-arg PULSE_LICENSE_SERVER_URL=...` (baked into `/etc/pulse/build-config.json`; not in `.env`) |
| `PULSE_LICENSE_PUBLIC_KEY` | Self-hosted | RSA public PEM matching `LICENSE_SIGNING_PRIVATE_KEY` on the license server (set in `.env` at deploy) |
| `LICENSE_SIGNING_PRIVATE_KEY` | License service (cloud compose) | RSA private PEM — license container only |
| `LICENSE_SERVER_API_KEY` | Cloud `.env` + license service | Shared secret for Paystack → `POST /api/v1/keys/purchase` (cloud API does not need this for normal SaaS tenants) |
| `DEPLOYMENT_MODE` | Cloud compose | `cloud` — enables Paystack subscriptions; `self_hosted` hides `/billing` subscription routes |
| `PAYSTACK_SECRET_KEY` | Cloud billing | Paystack secret key (test or live) |
| `PAYSTACK_PRO_PLAN_CODE` | Cloud billing | Paystack plan code for Pro recurring subscription |
| `PAYSTACK_GROWTH_PLAN_CODE` | Cloud billing | Paystack plan code for Growth tier (optional) |
| `PAYSTACK_SELFHOSTED_LICENSE_PRICE` | License sales | One-time license price in kobo |
| `BILLING_GRACE_DAYS` | Cloud billing | Days to keep paid access after failed renewal (default `7`) |

**Cloud Paystack setup:** Create recurring plans in the [Paystack dashboard](https://dashboard.paystack.com), set plan codes in env, and register webhook URL `https://<api-host>/api/v1/billing/webhook` (HMAC-SHA512 with your secret key). Customers upgrade via `POST /api/v1/billing/initialize` → Paystack checkout → `GET /api/v1/billing/verify/{reference}`. Update card: `GET /api/v1/billing/subscription/manage-link`.

See [`docker/compose/cloud/.env.example`](docker/compose/cloud/.env.example) for a cloud-focused template.

After deploy, run `alembic upgrade head` to apply migrations (including `subscriptions.payment_failed_at`).

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
├── images/pulse/           # Self-hosted all-in-one image
├── images/pulse-cloud/     # Cloud image (PULSE_SERVICE selects mode)
├── compose/self-hosted/    # Production self-host (db + qdrant + pulse)
└── compose/cloud/          # Dev / internal compose

scripts/db/
├── seed_telecom_db.py      # Demo data
└── reset_db.py             # Wipe DB
```

---

## Health Check

```bash
# Local API dev
curl http://localhost:8000/health

# Self-hosted (via nginx on port 80)
curl http://localhost/health
```

---

## Reference Docs

| File | What |
|---|---|
| [`docs/PAYSTACK_BILLING_SETUP.md`](docs/PAYSTACK_BILLING_SETUP.md) | **Paystack billing** — cloud subscriptions + self-hosted license (step-by-step + env) |
| [`docker/compose/self-hosted/README.md`](docker/compose/self-hosted/README.md) | **Self-hosted install** — `docker-compose.yml`, `.env.example`, quick start |
| `BACKEND_ROUTES.md` | Full endpoint reference |
| `SCHEMA.md` | Database schema (all 25 tables) |
| `MILESTONES.md` | Feature roadmap |
| `LICENSE_SYSTEM.md` | License key flow |
