# Entivia — Backend API

> **Connect your schema. Stream the actions.**
> Real‑time operational intelligence for any business.

[Entivia](https://entivia.online) links directly to your existing database — Postgres, MySQL, MSSQL, SQLite, and more — and runs an autonomous multi‑agent AI pipeline against it to turn relational tables into ranked recommendation queues, risk‑scored entity profiles, and live conversational workflows. **Your data never leaves your infrastructure**: Entivia introspects the schema and queries live, read‑only.

This repository is the backend (FastAPI + autonomous agent pipeline + license server). The web dashboard lives at [`PulseAgentZero/dashboard`](https://github.com/PulseAgentZero/dashboard). The published self‑hosted Docker image is at [`chideraozigbo488/entivia`](https://hub.docker.com/r/chideraozigbo488/entivia).

### Hackathon submission — DSN × Bluechip LLM Agent Challenge

**Built during the challenge (early May → 24 May 2026).** This entire repository — the Entivia platform in [`app/`](app/) *and* the formal competition entry in [`hackathon/`](hackathon/) — was written **from scratch within the official hackathon timeline**. That includes the agent runtime (`BaseAgent`, tool registry, JSON validation, provider fallback), the four-stage autonomous pipeline, the FastAPI API and auth/billing/license layers, the self-hosted and cloud Docker images, and then the Task A + Task B agents, Yelp/Goodreads loaders, eval harness, per-task containers, and solution papers. We did not port in a pre-existing product; we built Entivia as the engine for the challenge and shaped the submission on top of it.

**Why we structured it this way.** The brief asks for two containerized agents on review/persona data. Rather than a throwaway demo, we built a full agent platform first — connect to structured data, profile behaviour, score risk, recommend actions — and used the hackathon domain (Yelp + Goodreads) as the **public proof** that the runtime works: Task A is behavioural simulation, Task B is next-best-action recommendations, both on the same [`app/agents/base.py`](app/agents/base.py) stack as the enterprise pipeline described below.

**One codebase, two surfaces.** The two hackathon agents now live in `app/agents/workflows/` (`review_simulator.py`, `cold_start_recommender.py`) alongside the rest of the platform, and the same code is exposed two ways:

1. **Dedicated hackathon containers** — `task-a-api:8011` and `task-b-api:8012` from [`hackathon/`](hackathon/). These keep the full challenge contract: persona+product *or* `user_id`+`item_id` for Task A, warm-start + cold-start + multi-turn + cross-domain for Task B against the loaded Yelp/Goodreads slice. They are the submission's reference deployment.
2. **Production public API** — `POST /api/public/v1/simulation/review` and `POST /api/public/v1/simulation/recommend` on the main Entivia API, behind the standard `X-API-Key` auth and per-org rate limit, with `Simulation` endpoints surfaced in the dashboard's API Playground. Direct mode and cold-start mode only (the safe, tenant-agnostic paths).

| Layer | Path | Role in the submission |
|-------|------|-------------------------|
| Agent runtime + product API | [`app/`](app/) | ReAct agents, live-SQL tools, pipeline, simulation public routes |
| Hackathon Task A / B containers | [`hackathon/`](hackathon/) | `POST /simulate-review`, `POST /recommend`, eval, papers |

See [`hackathon/README.md`](hackathon/README.md) for `docker compose` quickstart, API examples, evaluation, and the two task papers.

| | |
|---|---|
| **Framework** | FastAPI · Python 3.12 · Pydantic v2 |
| **Database** | Async SQLAlchemy 2.0 · Postgres 16 · Alembic migrations |
| **Cache / queue** | Redis 7 · APScheduler |
| **Vector / search** | Qdrant · Voyage AI embeddings |
| **AI** | Anthropic Claude (primary) · Groq Llama 3.3 / GPT‑OSS (fallback) |
| **Auth** | JWT · Google OAuth · OIDC + SAML SSO · LDAP sync |
| **Billing** | Paystack one‑time + recurring |
| **Deploy** | Docker (multi‑arch) · cloud microservices or all‑in‑one self‑hosted image |

---

## Table of contents

1. [What Entivia does](#what-entivia-does)
2. [Architecture overview](#architecture-overview)
3. [User modeling & recommendation engine](#user-modeling--recommendation-engine)
4. [Supported data sources](#supported-data-sources)
5. [Project structure](#project-structure)
6. [Prerequisites](#prerequisites)
7. [Local development](#local-development)
8. [Docker — self‑hosted](#docker--self-hosted)
9. [Docker — cloud / internal dev](#docker--cloud--internal-dev)
10. [Environment variables](#environment-variables)
11. [Database migrations](#database-migrations)
12. [API documentation](#api-documentation)
13. [Make targets](#make-targets)
14. [Testing](#testing)
15. [Troubleshooting](#troubleshooting)

---

## What Entivia does

Most operational data sits in transactional tables that BI dashboards summarise but never act on. Entivia turns those tables into a continuously refreshed stream of decisions:

1. **Plain‑English business context.** An admin describes the operation in one paragraph (e.g. *"We're a telecom firm; risk means subscriber churn driven by balance drops"*). Entivia compiles this into agent guidance.
2. **Read‑only secure connection.** Encrypted DSN to the customer database, Fernet‑sealed at rest. TLS 1.3, read‑only role.
3. **Autonomous reasoning pipeline.** Four LLM‑backed agents run in sequence: schema intelligence → profiling → risk scoring → recommendation generation.
4. **Actionable output.** Risk‑tiered recommendation queue, conversational chat grounded in live SQL, automated webhooks/CRM dispatch, and audit‑logged human approval gates.

Industry packs are configurable rather than hard‑coded — telecom, healthcare, FMCG, and more all use the same engine with different schema mappings and risk thresholds.

---

## Architecture overview

We recognise that many organisations — banks, telcos, hospitals, regulated SaaS — simply will **not** allow customer data to leave their environment, no matter how strong the vendor's guarantees are. So Entivia ships the *same* codebase in two topologies, and the choice is yours:

- **Cloud (microservices) — Entivia‑managed.** For teams that want zero ops. Six independently scaled services — `api`, `worker` (pipeline), `agent` (conversational + SSE), `scheduler`, `license`, plus the dashboard — share Postgres / Redis / Qdrant. We operate it, we scale it, you sign in.
- **Self‑hosted (all‑in‑one) — runs in your VPC / on‑prem.** For teams with data‑residency, sovereignty, or compliance constraints (HIPAA, PCI, NDPR, GDPR, internal policy). Every Entivia service is bundled into one container behind nginx on port 80, deployed alongside your own Postgres + Qdrant. Your customer data never traverses our infrastructure — Entivia connects read‑only to your databases inside your network, and you control the LLM provider (including local Ollama for fully air‑gapped operation). Full setup guide: <https://docs.entivia.online/docs/hosting/self-hosted>.

![Entivia architecture](docker/images/pulse/architecture.jpeg)

---

## User modeling & recommendation engine

Entivia's core loop is a four‑agent pipeline (`app/agents/orchestrators/pipeline.py`) that turns raw client tables into a ranked, *acted‑upon* recommendation queue. Every step queries the customer database live and read‑only — Entivia never copies rows into its own store.

### 1. Behavioural profiling — *the "user model"*

`ProfilingAgent` (`app/agents/workflows/profiling_agent.py`, Groq `llama‑3.3‑70b‑versatile`) takes the entity table and related tables discovered in step 1 and issues cross‑table SQL through a tool registry to build a **behavioural profile per entity** (up to 200 per cycle by default):

- Usage cadence, balance / spend trajectory, support touchpoints, plan and tenure attributes, and any other signal mapped by the admin in `schema_mappings.signal_columns`.
- Each profile is summarised into a free‑text `profile_summary` plus a `behavioural_metrics` dict so it can be reasoned over downstream and surfaced in chat.
- Profiles are embedded with **Voyage AI** and indexed in **Qdrant** under the org's namespace (`embed_and_store_profiles`). They power semantic "find similar entities" lookups across every following cycle.
- Profiles are deliberately **never persisted** to the Pulse application database — they live in memory for the duration of the run and in the customer‑owned Qdrant index for retrieval.

### 2. Optional ML risk model

`ModelTrainingAgent` (Groq `gpt‑oss‑120b`) trains a scikit‑learn **Random Forest classifier** on the org's own historical data each cycle:

- Auto‑discovers the target variable (or honours `target_column` on the mapping), gathers a training dataframe via the same query tools, prepares features (excluding the entity ID / name columns), and trains.
- The trained model scores **every** entity into `[0.0, 1.0]`.
- Strict validation runs before the scores are used: accuracy must clear `0.55`, every score must lie in `[0, 1]`, IDs must be strings, and coverage must hit at least 50% of expected entities. Any failure flips `ml_available=False` and the pipeline transparently falls back to the deterministic rule‑based scorer — the cycle never aborts because of a bad model.

### 3. Risk scoring & narratives

`RiskScoringAgent` collapses ML predictions (when available) or deterministic `compute_risk()` output (using the per‑org `signal_columns` + `risk_config` thresholds) into a single ranked list, then re‑derives tiers from the score so there's a single source of truth:

| Score | Tier |
|---|---|
| ≥ 0.80 | `critical` |
| ≥ 0.60 | `high` |
| ≥ 0.40 | `medium` |
| < 0.40 | `low` |

For every elevated entity (≥ 0.6, capped at 50 per run) the agent generates a 1–2 sentence **risk narrative** with `gpt‑oss‑120b`, grounded in:

- the entity's own top signal values and Agent 2's behavioural profile,
- top‑K **similar past entities** retrieved from Qdrant (RAG), and
- ML feature importances when the model path is active.

Scored entities are re‑indexed into Qdrant with their fresh `risk_tier`, `risk_score`, and `last_scored_at` so the next cycle's retrieval can filter by tier and freshness.

### 4. Recommendation engine

`RecommendationAgent` (`app/agents/workflows/recommendation_agent.py`, `gpt‑oss‑120b`) turns the elevated list into a queue of business‑friendly, entity‑specific actions:

- **Slim payloads.** Each entity is reduced to plain‑English `key_facts`, an analyst note, and up to three **similar cases with their past recommendation history** (RAG). The LLM never sees feature importances or model jargon.
- **Per‑org RAG config.** Retrieval parameters are resolved per org from `schema_mappings.rag_config` so different industries can tune neighbourhood size, similarity floor, and freshness.
- **Batched generation.** 20 entities per LLM call with `repair_truncated_json` fallback and a deterministic template‑based fallback when a whole batch fails — every elevated entity is guaranteed an output.
- **Businessification pass.** A regex sweep strips any "feature importance %", "ML model", or "predicted chance of" phrasing that leaks through, before persisting.
- **Atomic queue swap.** Inside a single SQL `SAVEPOINT`, all currently `open` recommendations for the org are marked `superseded` and the new batch is inserted. A mid‑loop failure rolls the savepoint back, leaving the previous queue intact — the org is *never* left with zero active recommendations.
- **Notifications.** When the new batch contains `critical` / `high` urgency items, in‑app notifications are dispatched to org members through `notify_high_priority_recommendations`.

Every recommendation row carries `type`, `urgency`, `title`, `reasoning`, `suggested_action`, `expected_impact`, `status`, and the `pipeline_run_id` that produced it — so the dashboard, the conversational agent, and downstream CRM webhooks all share the same audited record.

### How it ties together

![Entivia agent pipeline flow](docker/images/pulse/pipeline-flow.png)

Pipeline metrics — duration, LLM calls, tool calls, tokens, provider fallbacks, RAG latency, and RAG‑eval regression scores — are persisted to `pipeline_runs` and mirrored to `logs/pipeline_runs/*.json` for every run, so quality regressions are visible the moment they happen.

Configuration entry points for the engine live on the schema mapping:

- `signal_columns` — which columns count as risk signals and what to call them in plain English.
- `risk_config` — per‑signal weights and thresholds used by the deterministic scorer.
- `target_column` — optional explicit target for the ML model; auto‑discovered otherwise.
- `rag_config` — per‑org retrieval knobs (top‑K, similarity floor, TTL).

---

## Supported data sources

Entivia connects to your existing systems through **connections** — credentials are encrypted at rest with Fernet, sessions run **read‑only** wherever the source supports it, and the pipeline queries live (no copy is taken). A condensed list of what's wired up today:

| Category | Connectors |
|---|---|
| **SQL databases** | PostgreSQL, MySQL / MariaDB, Microsoft SQL Server, Amazon Redshift, SQLite |
| **Cloud warehouses** | Snowflake, Google BigQuery, Databricks, ClickHouse |
| **NoSQL** | MongoDB |
| **Spreadsheets & SaaS** | Google Sheets, Airtable |
| **Object storage** | Amazon S3, Google Cloud Storage |
| **File upload** | CSV / TSV, Excel (`.xlsx`, `.xls`) |

Not every connector plays the same role: SQL databases and warehouses power the **full pipeline** (entity profiling, ML/rule scoring, recommendations) **and** Studio live SQL; CSV / Sheets / S3 (CSV objects) power **Studio in‑memory SQL** and pipeline ingestion; Airtable and MongoDB are document/API workflows and aren't usable as the live SQL entity source.

For the full per‑connector matrix — required fields, supported authentication methods, Studio vs pipeline support, and security recommendations — see the docs: <https://docs.entivia.online/docs/data-sources>.

---

## Project structure

```
api/
├── app/
│   ├── api/
│   │   ├── app.py                 # FastAPI factory — routers, middleware, error handlers
│   │   ├── auth/                  # JWT, login, signup, OAuth, SSO
│   │   ├── routes/                # Internal API   (/api/v1/*)
│   │   ├── public/                # Public API     (/api/public/v1/*)
│   │   ├── dependencies/          # Auth, role, plan-gate dependencies
│   │   └── errors.py              # Shared error helpers
│   │
│   ├── agents/                    # 4-stage autonomous pipeline + ReAct base agent
│   │   ├── orchestrators/         # PipelineOrchestrator
│   │   ├── tools/                 # Live-SQL + schema introspection tools
│   │   └── state.py               # Typed PipelineState
│   │
│   ├── conversational/            # /agent/* — chat agent (SSE) backed by live SQL tools
│   │
│   ├── services/                  # Business logic, schedulers, billing, license
│   │   ├── schedulers/            # APScheduler crons (pipeline, billing, prune…)
│   │   └── …
│   │
│   ├── infrastructure/
│   │   ├── database/
│   │   │   ├── models/            # ORM models (org, user, conn, recs, runs, …)
│   │   │   ├── repositories/      # Data-access layer
│   │   │   └── alembic/           # Migrations
│   │   ├── redis/                 # Client + token helpers
│   │   ├── crypto.py              # Fernet encryption for client DB credentials
│   │   ├── email/                 # Resend / SMTP backends + Jinja templates
│   │   └── license/               # Offline JWT verification of plc_… keys
│   │
│   ├── license_server/            # Standalone license-issuance + validation API
│   ├── worker.py                  # Pipeline worker entrypoint
│   └── config/
│       └── settings.py            # All env-var configuration
│
├── docker/
│   ├── images/pulse/              # Self-hosted all-in-one image (Dockerfile, nginx, supervisord)
│   ├── images/pulse-cloud/        # Cloud image — PULSE_SERVICE selects api|worker|agent|scheduler
│   ├── images/pulse-license/      # License-server image
│   ├── compose/self-hosted/       # End-user compose (db + qdrant + pulse)
│   └── compose/cloud/             # Internal cloud compose
│
├── scripts/
│   ├── db/seed_telecom_db.py      # Demo telecom data
│   └── db/reset_db.py             # Wipe DB
│
├── tests/                         # Pytest smoke tests
├── deploy/                        # VPS bootstrap + nginx config
├── alembic.ini                    # Migration config
├── Makefile                       # Common dev / build / push targets
├── requirements.txt               # Pinned Python deps
└── README.md
```

---

## Prerequisites

- **Python 3.12+** (matches the production Docker image — `python:3.12-slim`)
- **Postgres 16+** (Entivia's own metadata DB)
- **Redis 7+** (queues, refresh tokens, rate limits, public‑API limits)
- **Qdrant** (optional locally; required for semantic entity search and RAG)
- **Docker + Docker Compose** for any deployment path
- At least one **LLM key**: Anthropic (recommended) or Groq (fallback)

---

## Local development

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — at minimum fill DATABASE_URL, JWT_SECRET, ENCRYPTION_KEY, ANTHROPIC_API_KEY (or GROQ_API_KEY)

# 3. Apply database migrations
alembic upgrade head

# 4. Start the API with hot reload
uvicorn app.api.app:app --reload --host 0.0.0.0 --port 8000

# 5. (Separate terminal) Start the background scheduler — pipeline runs, billing, memory prune, etc.
make dev-scheduler
```

The API process **does not** start cron jobs in its lifespan. Pipeline runs, license revalidation, billing, memory prune, and similar work all live in `app.services.schedulers.run`, which mirrors the cloud `scheduler` service and the self‑hosted supervisord process. Run it alongside the API in dev.

Generate the two required cryptographic secrets:

```bash
# JWT_SECRET
openssl rand -hex 32

# ENCRYPTION_KEY (Fernet — encrypts client DB credentials at rest)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Docker — self‑hosted

> Full local / self‑hosted setup guide: <https://docs.entivia.online/docs/hosting/self-hosted>

A single pre‑built image (`chideraozigbo488/entivia`) bundles UI, API, agent, worker, scheduler, Redis, and nginx behind port 80. Customers bring their own Postgres and Qdrant.

```bash
cd docker/compose/self-hosted
cp .env.example .env
# Fill at minimum: POSTGRES_PASSWORD, JWT_SECRET, ENCRYPTION_KEY, and one LLM key
docker compose pull
docker compose up -d
open http://localhost
```

From the repo root the same flow is wrapped in Make: `make sh-pull && make sh-up`, then `make sh-logs`.

The Docker Hub overview for end users lives at [`docker/images/pulse/DOCKERHUB.md`](docker/images/pulse/DOCKERHUB.md). Maintainer build instructions are in [`docker/images/pulse/Dockerfile`](docker/images/pulse/Dockerfile).

---

## Docker — cloud / internal dev

Runs every service as its own container — `api`, `worker`, `agent`, `scheduler`, `license`, plus Postgres / Redis / Qdrant. This mirrors the production cloud topology exactly, so it's the right environment for benchmarking and integration tests.

```bash
cp docker/compose/cloud/.env.example docker/compose/cloud/.env
docker compose -f docker/compose/cloud/docker-compose.yml up --build -d
```

> To pull the published image instead of building from source, comment out the `build:` block in `docker/compose/cloud/docker-compose.yml`.

Common operations:

```bash
docker compose -f docker/compose/cloud/docker-compose.yml logs -f
docker compose -f docker/compose/cloud/docker-compose.yml restart api
docker compose -f docker/compose/cloud/docker-compose.yml up --scale worker=4 -d
docker compose -f docker/compose/cloud/docker-compose.yml down -v
```

---

## Environment variables

Copy [`.env.example`](.env.example) to `.env` and fill in the values relevant to your deployment mode.

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | yes | Postgres — `postgresql+asyncpg://user:pass@host:5432/db` |
| `JWT_SECRET` | yes | Token signing — `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | yes | Fernet key — encrypts client DB credentials at rest |
| `ANTHROPIC_API_KEY` | yes\* | Primary LLM (Claude) |
| `GROQ_API_KEY` | yes\* | Fallback LLM (Groq) |
| `REDIS_URL` | cloud / dev | `redis://localhost:6379/0` — bundled in the self‑hosted image |
| `RESEND_API_KEY` | no | Resend API key for verify / reset / receipt emails |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | no | Google OAuth sign‑in |
| `VOYAGEAI_API_KEY` | no | Embeddings for semantic entity search and RAG |
| `QDRANT_URL` | self‑hosted compose | Defaulted to `http://qdrant:6333` by compose |
| `FRONTEND_URL` | no | Public origin of the dashboard — required for CORS + auth email links (default `http://localhost:3000`) |
| `LICENSE_SERVER_URL` | self‑hosted image | Set at **image build** via `--build-arg PULSE_LICENSE_SERVER_URL=…`. Baked into `/etc/pulse/build-config.json`; not in `.env`. |
| `PULSE_LICENSE_PUBLIC_KEY` | self‑hosted image | RSA public PEM passed via `--build-arg` at image build. Customers do not set this. |
| `LICENSE_SIGNING_PRIVATE_KEY` | license service (cloud compose) | RSA private PEM — license container only |
| `LICENSE_SERVER_API_KEY` | license service | Shared secret for `POST /api/v1/keys/purchase` (Paystack → license server) |
| `DEPLOYMENT_MODE` | cloud compose | `cloud` enables Paystack subscriptions; `self_hosted` hides `/billing` subscription routes |
| `PAYSTACK_SECRET_KEY` | cloud billing | Test or live Paystack secret key |
| `PAYSTACK_PRO_PLAN_CODE` | cloud billing | Plan code for the Pro recurring subscription |
| `PAYSTACK_GROWTH_PLAN_CODE` | cloud billing | Plan code for the Growth tier (optional) |
| `PAYSTACK_SELFHOSTED_LICENSE_PRICE` | license sales | One‑time license price in kobo |
| `BILLING_GRACE_DAYS` | cloud billing | Days of paid access kept after a failed renewal (default `7`) |

\* At least one of `ANTHROPIC_API_KEY` or `GROQ_API_KEY` must be set. Both is recommended — Entivia automatically falls back to the other on rate limit / outage.

In production, setting `DATABASE_URL_SECRET_ARN` triggers AWS Secrets Manager lookup instead of using `DATABASE_URL` directly.

A cloud‑specific template lives at [`docker/compose/cloud/.env.example`](docker/compose/cloud/.env.example); the self‑hosted user template is at [`docker/compose/self-hosted/.env.example`](docker/compose/self-hosted/.env.example).

**Cloud Paystack setup.** Create recurring plans in the [Paystack dashboard](https://dashboard.paystack.com), set the plan codes in env, and register webhook URL `https://<api-host>/api/v1/billing/webhook` (HMAC‑SHA512 with your secret key). Customers upgrade via `POST /api/v1/billing/initialize` → Paystack checkout → `GET /api/v1/billing/verify/{reference}`. Card management: `GET /api/v1/billing/subscription/manage-link`. Step‑by‑step instructions live in [`docs/PAYSTACK_BILLING_SETUP.md`](docs/PAYSTACK_BILLING_SETUP.md).

---

## Database migrations

```bash
alembic upgrade head                              # apply all
alembic revision --autogenerate -m "description"  # new migration
alembic downgrade -1                              # roll back one
```

The Docker image runs `alembic upgrade head` automatically on container start, so manual migrations are only needed for local dev.

---

## API documentation

| URL | What |
|---|---|
| `http://localhost:8000/docs` | Swagger UI — interactive (dev only) |
| `http://localhost:8000/redoc` | ReDoc — shareable, always on |
| `http://localhost:8000/openapi.json` | OpenAPI spec — import into Postman / Scalar |
| `http://localhost:8000/api/public/redoc` | Public API docs (the customer‑facing v1) |

Full route reference: [`BACKEND_ROUTES.md`](BACKEND_ROUTES.md).

---

## Make targets

The [`Makefile`](Makefile) wraps the most common workflows:

| Target | What it does |
|---|---|
| `make dev` | Start API with hot reload on `:8000` |
| `make dev-scheduler` | Start the background scheduler process |
| `make migrate` | `alembic upgrade head` |
| `make revision name="..."` | Create a new auto‑generated migration |
| `make seed` | Seed demo telecom data into the metadata DB |
| `make reset-db` | Drop + recreate metadata schema |
| `make sh-up` / `sh-down` / `sh-logs` / `sh-pull` | Self‑hosted compose lifecycle |
| `make up` / `down` / `logs svc=api` | Cloud compose lifecycle |
| `make scale-workers n=4` | Scale pipeline workers in cloud compose |
| `make build-self-hosted` | Build the all‑in‑one image (uses `PULSE_DASHBOARD_DIR` for the dashboard build context — defaults to `../dashboard`) |
| `make build-cloud` / `build-license` | Build cloud + license images |
| `make push-self-hosted` / `push-cloud` / `push-license` | Push to Docker Hub |

---

## Testing

```bash
pytest tests/test_smoke.py -q
```

Smoke tests cover health, auth envelope, and public‑route validation. They don't require a database for the default paths.

---

## Troubleshooting

**`/health` returns 502 for the first ~90 s after `docker compose up`.**
Normal — the container runs migrations and warms the agent on first boot. The healthcheck has a 90 s `start_period`.

**`License key signature could not be verified` in self‑hosted.**
The Docker image was built with a different `PULSE_LICENSE_PUBLIC_KEY` than the license server's `LICENSE_SIGNING_PRIVATE_KEY`. Regenerate the public key from the private key, rebuild the image, and redeploy. The license activation flow now also falls back to the live license server, so existing customers stay unlocked during the rebuild.

**Pipeline shows "Scheduler not seen recently".**
The scheduler subprocess exited. Check `docker compose logs entivia | grep scheduler`. Most often: missing or invalid LLM key, or DB connectivity flaked at startup. `docker compose restart entivia` recovers it.

**OAuth / verify / reset email links go to the wrong host.**
The API's `FRONTEND_URL` must match the dashboard's public origin exactly (scheme + host + port).

**Out of memory on a small VPS.**
Set `REDIS_MAX_MEMORY=128mb`, `API_WORKERS=1`, `AGENT_WORKERS=1` in `.env`. The minimum comfortable size is 2 vCPU / 4 GB RAM.

---

## License

[MIT](LICENSE) © 2026 PulseAgentZero. The Entivia software is the open, self‑hostable operational intelligence layer behind <https://entivia.online>. Cloud subscriptions and signed self‑hosted Pro licenses ship paid features and SLAs on top of the same codebase — see <https://entivia.online/pricing>.

---

## Links

- Website: <https://entivia.online>
- Docs: <https://entivia.online/docs>
- Frontend repo: <https://github.com/PulseAgentZero/dashboard>
- Self‑hosted Docker image: <https://hub.docker.com/r/chideraozigbo488/entivia>
- Support: <support@entivia.online>

*Built for the **DSN × Bluechip Technologies Challenge 3.0** by the [PulseAgentZero](https://github.com/PulseAgentZero) team.*
