# DSN × Bluechip LLM Agent Challenge 3.0 — Submission

This repository is our entry for the **DSN × Bluechip Technologies LLM Agent Challenge 3.0** (May 2026).

It contains:

- **[`hackathon/`](hackathon/)** — the formal challenge entry. Two containerized agents (**Task A: review simulation**, **Task B: recommendations**), a Yelp + Goodreads loader, an evaluation harness, two solution papers, and a single `docker compose` file that brings it all up.
- **[`app/`](app/)** — the agent runtime the two challenge agents are built on. After the agents were working, we wrapped this runtime in a small multi-tenant SaaS so the work could keep going past the deadline. That part is optional context — *the hackathon work is what's being submitted*.

Everything in this repo — both folders — was written **from scratch within the official hackathon timeline (early May → 24 May 2026)**.

> **Judges:** the fastest path to evaluation is [`hackathon/README.md`](hackathon/README.md). It has the `docker compose` quickstart, API examples for both tasks, evaluation metrics, deployment notes, and links to the two solution papers.

---

## Submission at a glance

| Service | Port | Endpoint | Task |
|---|---|---|---|
| `task-a-api` | **8011** | `POST /simulate-review` | **Task A** — persona + product → star rating + review text |
| `task-b-api` | **8012** | `POST /recommend` | **Task B** — warm / cold-start / multi-turn / cross-domain recommendations |
| `hackathon-api` | 8010 | both endpoints + `GET /metrics` | Optional combined gateway for local demos |

| Challenge requirement | Where it lives |
|---|---|
| Containerized application per task | [`hackathon/docker-compose.yml`](hackathon/docker-compose.yml) — `task-a-api`, `task-b-api` |
| Task A input: persona + product | `POST /simulate-review` accepts `{persona, product}` (direct mode) **or** `{user_id, item_id}` (DB mode against the Yelp slice) |
| Task B input: persona built on review history | `POST /recommend` reads the Yelp persona slice loaded for Task A |
| Cross-domain demonstration | `dataset=goodreads` in `POST /recommend` |
| Nigerian English / Pidgin localization | `voice=nigerian` in `POST /simulate-review` |
| Two solution papers (PDF) | [`hackathon/paper/task_a_review_simulation.pdf`](hackathon/paper/task_a_review_simulation.pdf), [`hackathon/paper/task_b_recommendation.pdf`](hackathon/paper/task_b_recommendation.pdf) |
| Evaluation metrics | [`hackathon/eval/data/EVAL.md`](hackathon/eval/) + `GET /metrics` |
| Submission checklist | [`hackathon/SUBMISSION.md`](hackathon/SUBMISSION.md) |

Both task containers expose Swagger UI at `/docs` and a health probe at `/healthz`.

---

## Quickstart for judges

```bash
git clone <this-repo>
cd api

cp hackathon/.env.example hackathon/.env
# Add ANTHROPIC_API_KEY (and optionally GROQ_API_KEY for automatic fallback)

make hackathon-up                                                # postgres + qdrant + 3 api containers
HACKATHON_YELP_HOST_DIR=~/datasets/yelp make hackathon-load      # ingest Yelp + Goodreads + embeddings

open http://localhost:8011/docs   # Task A — review simulation
open http://localhost:8012/docs   # Task B — recommendations
```

A worked example of Task A in **direct mode** (the challenge-spec input — no DB needed):

```bash
curl -s http://localhost:8011/simulate-review \
  -H 'Content-Type: application/json' \
  -d '{
    "persona": {
      "description": "Generous reviewer who loves spicy Nigerian food.",
      "avg_stars": 4.2,
      "top_categories": ["Nigerian", "Restaurants"]
    },
    "product": {
      "name": "Tam Tam African Restaurant",
      "categories": "African, Restaurants",
      "city": "Philadelphia"
    },
    "voice": "default"
  }' | jq
```

Full per-task API examples, evaluation steps, and deployment details: [`hackathon/README.md`](hackathon/README.md).

---

## Architecture of the submission

![Hackathon architecture](hackathon/paper/architecture.png)

- **`BaseAgent`** ReAct loop — JSON-validated outputs, tool registry, Anthropic primary + Groq fallback. Implementation: [`app/agents/base.py`](app/agents/base.py).
- **Task A agent** — [`app/agents/workflows/review_simulator.py`](app/agents/workflows/review_simulator.py). Prompts in `app/agents/workflows/prompts/simulation/`.
- **Task B agent** — [`app/agents/workflows/cold_start_recommender.py`](app/agents/workflows/cold_start_recommender.py). Uses fastembed (BGE-small) for embeddings and Qdrant for ANN.
- **Shared Pydantic contracts** — [`app/api/schemas/simulation.py`](app/api/schemas/simulation.py).
- **Per-request observability** — every successful response carries a `meta` block with model, providers used, LLM call count, tool call count, prompt/completion tokens, latency, and retrieval stats. Source: [`app/agents/observability.py`](app/agents/observability.py).
- **Hackathon wrappers** — [`hackathon/agents/*.py`](hackathon/agents/) are thin shims that wire the DB-mode tools (Yelp `user_id`/`item_id` lookups) onto the production agents. The challenge ASGI apps are [`hackathon/app/task_a.py`](hackathon/app/task_a.py) and [`hackathon/app/task_b.py`](hackathon/app/task_b.py).

The agent code is deliberately platform-style (classes, schemas, prompts as files, observability, lazy DI) instead of one-off scripts, because that's how it gets to be reused beyond the hackathon (see next section).

---

## Repository layout

```text
api/
├── hackathon/                       # ← The challenge entry. Read hackathon/README.md.
│   ├── app/                         # Per-task FastAPI apps (task_a, task_b, main)
│   ├── agents/                      # Thin shims onto the production agents
│   ├── core/                        # db session, repository, fastembed wrapper, Qdrant wrapper
│   ├── data/                        # Yelp + Goodreads loaders
│   ├── eval/                        # RMSE / ROUGE-L / Hit@K / NDCG harness
│   ├── paper/                       # Task A + Task B solution papers + architecture script
│   ├── docker-compose.yml           # postgres + qdrant + task-a + task-b + gateway
│   ├── Dockerfile                   # Same image used by all three api services
│   └── README.md                    # ← judges read this
│
└── app/                             # The agent runtime + platform layer
    ├── agents/
    │   ├── base.py                  # BaseAgent (ReAct, JSON validation, provider fallback)
    │   ├── workflows/               # Promoted hackathon agents + platform pipeline agents
    │   │   ├── review_simulator.py
    │   │   ├── cold_start_recommender.py
    │   │   └── prompts/simulation/  # Default + Nigerian voice prompts
    │   └── observability.py         # Per-request meta helper
    ├── api/
    │   ├── public/simulation.py     # Public routes that call the same agents
    │   └── schemas/simulation.py    # Shared request/response contracts
    └── infrastructure/              # Postgres, Redis, Qdrant, crypto, email
```

---

## Taking it further — wrapping the agents in a platform

After both task containers were working, we wrapped the same agent runtime in a small multi-tenant SaaS so the project could keep going past the deadline. Concretely:

- **The same two agents are exposed as a first-class public API** on the main backend in `app/`, behind `X-API-Key` auth and per-org rate limits.
- **The dashboard's API Playground** has a *Simulation* group that calls those endpoints with pre-filled JSON bodies, so the challenge agents can be exercised inside a real product UI as well as the containers.
- The platform also runs a 4-stage autonomous pipeline (schema introspection → behavioural profiling → risk scoring → recommendation) over arbitrary customer databases. That part is outside the challenge brief; it shares the same `BaseAgent` runtime as Task A and Task B and is here to show the runtime composes.

This is "we took it further" work, not the deliverable. The deliverable is `hackathon/`. Platform documentation: <https://docs.entivia.online>. Public website: <https://entivia.online>.

### Production API endpoints

The platform is live at `https://api.entivia.online`. The challenge agents are exposed under the public API at:

| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/api/public/v1/simulation/review` | Task A — predicts star rating + review text from a `persona + product` payload |
| `POST` | `/api/public/v1/simulation/recommend` | Task B — cold-start recommendations from a free-text `persona` (supports `dataset=goodreads`, `conversation_id` for multi-turn) |

Auth is `X-API-Key: <key>` (generate one in the dashboard). The full public API also exposes entities, recommendations, pipeline runs, analytics, and Studio dashboards — see the live ReDoc:

- **Public API docs (ReDoc):** <https://api.entivia.online/api/public/redoc>
- **Public API OpenAPI:** <https://api.entivia.online/api/public/openapi.json>

The hackathon containers themselves are also deployed for direct judging:

| Surface | URL |
|---|---|
| Task A — Swagger | <https://hackathon-a.entivia.online/docs> |
| Task B — Swagger | <https://hackathon-b.entivia.online/docs> |
| Combined gateway | <https://hackathon.entivia.online/docs> |

Example call against the live platform's Task A endpoint:

```bash
curl -s https://api.entivia.online/api/public/v1/simulation/review \
  -H 'X-API-Key: YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "persona": {
      "description": "Generous reviewer who loves spicy Nigerian food.",
      "avg_stars": 4.2,
      "top_categories": ["Nigerian", "Restaurants"]
    },
    "product": {
      "name": "Tam Tam African Restaurant",
      "categories": "African, Restaurants",
      "city": "Philadelphia"
    },
    "voice": "default"
  }' | jq
```

---

## Running the platform locally (optional)

You only need this if you want to play with the wider platform on top of the challenge containers.

Prerequisites:

- Python 3.12+
- Postgres 16+ · Redis 7+ · Qdrant
- One LLM key — Anthropic (recommended) or Groq (fallback)

Non-Docker dev:

```bash
pip install -r requirements.txt
cp .env.example .env                # fill DATABASE_URL, JWT_SECRET, ENCRYPTION_KEY, ANTHROPIC_API_KEY
alembic upgrade head
uvicorn app.api.app:app --reload --host 0.0.0.0 --port 8000
make dev-scheduler                  # second terminal — APScheduler crons
```

Docker:

```bash
# Self-hosted (single image — what customers deploy)
make sh-pull && make sh-up && make sh-logs

# Cloud / dev (microservices — mirrors production)
docker compose -f docker/compose/cloud/docker-compose.yml up --build -d
```

Generate the two required secrets:

```bash
openssl rand -hex 32                                                                # JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # ENCRYPTION_KEY (Fernet)
```

The full env-var matrix, billing setup, SSO / LDAP / log-stream extras, and per-image build args are documented at <https://docs.entivia.online> rather than here so this README stays focused on the submission.

---

## Tech stack

| | |
|---|---|
| **Framework** | FastAPI · Python 3.12 · Pydantic v2 |
| **Database** | Async SQLAlchemy 2.0 · Postgres 16 · Alembic |
| **Cache / queue** | Redis 7 · APScheduler |
| **Vector / embeddings** | Qdrant · fastembed (BGE-small) · Voyage AI |
| **LLMs** | Anthropic Claude (primary) · Groq Llama 3.3 / GPT-OSS (fallback) |
| **Containers** | Docker (multi-arch); per-task images from one Dockerfile |

---

## License

[MIT](LICENSE) © 2026 PulseAgentZero. Built for the **DSN × Bluechip Technologies Challenge 3.0** by the [PulseAgentZero](https://github.com/PulseAgentZero) team.

---

## Links

- Hackathon entry: [`hackathon/README.md`](hackathon/README.md)
- Submission checklist: [`hackathon/SUBMISSION.md`](hackathon/SUBMISSION.md)
- Task A paper (PDF): [`hackathon/paper/task_a_review_simulation.pdf`](hackathon/paper/task_a_review_simulation.pdf)
- Task B paper (PDF): [`hackathon/paper/task_b_recommendation.pdf`](hackathon/paper/task_b_recommendation.pdf)
- Task A live API: <https://hackathon-a.entivia.online/docs>
- Task B live API: <https://hackathon-b.entivia.online/docs>
- Public API ReDoc (platform): <https://api.entivia.online/api/public/redoc>
- Platform website (the "taking it further" part): <https://entivia.online>
- Platform docs: <https://docs.entivia.online>
- Frontend repo: <https://github.com/PulseAgentZero/dashboard>
- Self-hosted Docker image: <https://hub.docker.com/r/chideraozigbo488/entivia>
- Support: <support@entivia.online>
