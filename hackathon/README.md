# DSN × Bluechip LLM Agent Challenge 3.0 — Submission

**Team Entivia · May 2026**

This repository contains our submission for both tasks of the challenge:

- **Task A — Review Simulation Agent** — predicts a star rating and review text
  from a user persona and a product.
- **Task B — Recommendation Agent** — produces personalized recommendations
  from a user persona built on review history.

Both agents are shipped as **two Docker containers built from the same image**,
with a Yelp Open Dataset slice and a Goodreads cross-domain demo. A Nigerian
English voice variant is included for the localization signal.

---

## Why this is built on the Entivia runtime

Although this submission is a self-contained hackathon project, it reuses the
agent runtime, database tooling, and observability layer from
[entivia.online](https://entivia.online) — our live multi-tenant agent
platform. The shared building blocks are:

| Capability | Used here as |
|------------|-------------|
| **`BaseAgent`** ReAct loop with provider fallback and JSON validation | The runtime behind both `ReviewSimulationAgent` and `RecommendationAgent` |
| Tool-backed live database access | `fetch_user_profile`, `fetch_item`, `fetch_user_history`, `ann_search_items` |
| Per-request observability (`meta`) | Returned on every successful response |
| Containerized deployment, env-driven config | `Dockerfile` + `docker-compose.yml` with one service per task |

Yelp/Goodreads here play the same role that a customer's connected database
plays in the production platform: a structured source of behavioral data the
agents reason over without copying it elsewhere. The hackathon code lives in
`hackathon/`; the reused runtime lives in `app/`.

---

## Submission at a glance

| Service | Port | Endpoint | Task |
|---------|------|----------|------|
| `task-a-api` | **8011** | `POST /simulate-review` | **Task A** — persona + product → star rating + review text |
| `task-b-api` | **8012** | `POST /recommend` | **Task B** — warm / cold-start / multi-turn / cross-domain recommendations |
| `hackathon-api` | 8010 | both endpoints + `GET /metrics` | Optional combined gateway for local demos |

Swagger UI on every container at `/docs`. Health probe at `/healthz`.

| Challenge requirement | Where it lives |
|------------------------|----------------|
| Containerized application per task | `docker-compose.yml` → `task-a-api`, `task-b-api` |
| Task A input: persona + product | `POST /simulate-review` accepts `{persona, product}` |
| Task B input: persona dataset built on review history | `POST /recommend` reads the Yelp persona slice loaded for Task A |
| Cross-domain demonstration | `dataset=goodreads` in `POST /recommend` |
| Nigerian English / Pidgin variant | `voice=nigerian` in `POST /simulate-review` |
| Two solution papers | `paper/task_a_review_simulation.md`, `paper/task_b_recommendation.md` |
| Evaluation metrics | `eval/data/EVAL.md` + `GET /metrics` |
| Code repository | This monorepo — see `hackathon/` |
| Live demo | <https://hackathon.entivia.online> |
| Submission checklist | `SUBMISSION.md` |

---

## 1. Repository layout

```text
hackathon/
├── app/
│   ├── task_a.py              # Task A ASGI app          → container: task-a-api
│   ├── task_b.py              # Task B ASGI app          → container: task-b-api
│   ├── main.py                # Combined gateway         → container: hackathon-api
│   ├── factory.py             # Shared FastAPI/CORS/logging factory
│   └── schemas.py             # Pydantic request/response models
├── agents/
│   ├── review_simulator.py    # ReviewSimulationAgent (Task A)
│   ├── recommender.py         # RecommendationAgent   (Task B)
│   └── prompts/               # System prompts (default + Nigerian voice)
├── core/
│   ├── db.py                  # Async SQLAlchemy session
│   ├── repository.py          # Postgres queries used by agent tools
│   ├── embeddings.py          # fastembed (BAAI/bge-small-en-v1.5) wrapper
│   ├── vector_store.py        # Qdrant client + upsert / search helpers
│   └── observability.py       # Per-request `meta` (tokens, latency, tool calls)
├── data/
│   ├── load.py                # Loader entrypoint (Postgres + Qdrant)
│   ├── yelp.py · goodreads.py · synthetic.py
│   ├── embed.py               # Item-embedding job
│   └── schema.sql             # users / items / reviews tables
├── eval/
│   ├── run.py                 # RMSE, ROUGE-L, Hit@10, NDCG@10 + baselines
│   ├── metrics.py             # Parser for the /metrics endpoint
│   └── data/                  # holdout_yelp.jsonl + generated EVAL.md
├── paper/
│   ├── task_a_review_simulation.md
│   ├── task_b_recommendation.md
│   └── solution_paper.md      # Index linking both papers
├── Dockerfile                 # Single image used by all task containers
├── docker-compose.yml         # postgres · qdrant · task-a · task-b · gateway · loader
├── requirements.txt
└── SUBMISSION.md              # Final submission checklist
```

`app/` (sibling directory at the repo root) is the reused Entivia runtime.
The hackathon code only depends on `app/agents/base.py` and a handful of
related helpers.

---

## 2. Reproducing the submission

**Prerequisites:** Docker, Docker Compose, and an `ANTHROPIC_API_KEY`
(Groq is an optional automatic fallback).

```bash
cp .env.example .env
$EDITOR .env                        # ANTHROPIC_API_KEY=...  (GROQ_API_KEY optional)

make hackathon-up                   # postgres · qdrant · task-a · task-b · gateway
make hackathon-load                 # synthetic by default; real Yelp if mounted (§4)

open http://localhost:8011/docs     # Task A
open http://localhost:8012/docs     # Task B
```

Stop the stack with `make hackathon-down`. Stream logs with `make hackathon-logs`.

---

## 3. API reference

### 3.1 Task A — `POST /simulate-review`

**Direct mode (challenge specification).** Provide a `persona` object and a
`product` object; receive a predicted star rating and review text. No database
required.

```bash
curl -s http://localhost:8011/simulate-review \
  -H 'Content-Type: application/json' \
  -d '{
    "persona": {
      "description": "Generous reviewer who loves spicy Nigerian food and casual cafes.",
      "avg_stars": 4.2,
      "top_categories": ["Nigerian", "Restaurants"],
      "sample_reviews": ["The jollof was fire — generous portions, quick service."]
    },
    "product": {
      "name": "Tam Tam African Restaurant",
      "categories": "African, Restaurants",
      "city": "Philadelphia",
      "stars": 3.5
    },
    "voice": "default"
  }'
```

**Database mode (demonstrates grounding in the loaded Yelp slice).** Provide a
real `user_id` and `item_id`; the agent calls `fetch_user_profile` and
`fetch_item` to ground the prediction.

```bash
curl -s 'http://localhost:8011/samples/users?limit=1'   # discover real ids

curl -s http://localhost:8011/simulate-review \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"_BcWyKQL16ndpBdggh2kNA","item_id":"EoQiJ5D-pyWczjElN24oZg"}'
```

`voice` may be `default` or `nigerian`. The Nigerian variant uses Nigerian
English / light Pidgin while preserving the same grounding rules — controlled
style transfer, not a different task.

### 3.2 Task B — `POST /recommend`

Reads from the same persona dataset that Task A operates on.

| Mode | Request shape |
|------|---------------|
| Warm-start | `{"user_id": "...", "k": 5, "dataset": "yelp"}` |
| Cold-start | `{"persona": "loves spicy Nigerian food", "k": 5}` |
| Multi-turn | `{"conversation_id": "<from previous call>", "follow_up": "make these cheaper"}` |
| Cross-domain | `{"persona": "African literary fiction", "k": 5, "dataset": "goodreads"}` |

```bash
curl -s http://localhost:8012/recommend \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"_BcWyKQL16ndpBdggh2kNA","k":5,"dataset":"yelp"}'
```

### 3.3 Metrics — `GET /metrics`

Returns the most recent evaluation snapshot as JSON, parsed from
`eval/data/EVAL.md`. Served by either task container.

```bash
curl -s http://localhost:8011/metrics
```

---

## 4. Datasets

This submission uses public review datasets that fit the challenge spec.

### Yelp (primary)

1. Download the [Yelp Open Dataset](https://www.yelp.com/dataset) and unzip
   the `yelp_academic_dataset_*.json` files into one folder (default:
   `~/datasets/yelp/`).
2. Mount and load:

```bash
export HACKATHON_YELP_HOST_DIR=~/datasets/yelp
make hackathon-load
```

The Makefile mounts that folder read-only into the loader container.

Without the dataset, `HACKATHON_ALLOW_SYNTHETIC=1` (default) generates a
reproducible restaurant slice so the agents still run end-to-end. This is the
expected configuration for judges who cannot download the full Yelp dump.

Defaults (tunable via environment variables): 5,000 users with ≥ 10 reviews
each, up to 12,000 businesses, 10 % per-user holdout reserved for evaluation.

### Goodreads (cross-domain demo)

Used only to show Task B working on a different item domain. A small synthetic
Goodreads slice is generated automatically; no download is required.

---

## 5. Evaluation

```bash
# ANN + average-stars baseline (no LLM cost; fast smoke check)
docker compose -f hackathon/docker-compose.yml run --rm hackathon-api \
  python -m hackathon.eval.run --skip-llm --task-b-users 100

# Full agent eval (uses ANTHROPIC_API_KEY)
make hackathon-eval

# Larger sweep
docker compose -f hackathon/docker-compose.yml run --rm hackathon-api \
  python -m hackathon.eval.run --task-a-sample 300 --task-b-users 200
```

Results are written to `hackathon/eval/data/EVAL.md` and exposed through
`GET /metrics`.

| Task | Metric | Interpretation |
|------|--------|----------------|
| A | **RMSE** ↓ | Predicted star rating vs ground-truth review |
| A | **ROUGE-L** ↑ | Lexical overlap of generated vs real review text |
| B | **Hit@10** ↑ | Any held-out positive present in the top-10 |
| B | **NDCG@10** ↑ | Graded relevance ranking |
| Both | **Baselines** | Average-stars (Task A); ANN-only retrieval (Task B) |

---

## 6. Per-response observability

Every successful agent response includes a `meta` block — useful for grading,
debugging, and SLA tracking:

```json
"meta": {
  "agent": "review_simulator",
  "model": "claude-sonnet-4-6",
  "primary_provider": "anthropic",
  "providers_used": ["anthropic"],
  "llm_calls": 1,
  "tool_calls": 0,
  "prompt_tokens": 1099,
  "completion_tokens": 93,
  "latency_ms": 5160,
  "validation_retries": 0,
  "provider_fallbacks": 0,
  "task": "review_simulation",
  "input_mode": "direct",
  "voice": "default"
}
```

Task B additionally reports `embedding_backend`, `candidate_pool_size`,
`top_ann_score`, and `excluded_items_count`.

---

## 7. Architecture

![Architecture diagram](paper/architecture.png)

End-to-end flow:

1. **Loaders** (`hackathon/data/`) stream the Yelp Open Dataset (and an optional
   Goodreads slice) into Postgres.
2. **`fastembed`** (BAAI/bge-small-en-v1.5, 384-d) embeds item text locally —
   no embedding API, fully offline once the model is cached in a Docker volume.
3. **Qdrant** stores item vectors and per-user persona vectors used by Task B.
4. **Task A** (`task-a-api`, port 8011) runs `ReviewSimulationAgent`. In direct
   mode it works from the request payload alone; in DB mode it grounds via
   `fetch_user_profile` and `fetch_item`.
5. **Task B** (`task-b-api`, port 8012) runs `RecommendationAgent`. It uses
   ANN retrieval (`ann_search_items`) plus `fetch_user_history` and
   `fetch_item` for grounded re-ranking.
6. Both agents share the same `BaseAgent` runtime — a ReAct loop with
   Anthropic Claude as primary, Groq as automatic fallback, and strict JSON
   output validation.

Both task containers ship from the **same image** (`entivia-hackathon:latest`)
with different `uvicorn` commands, satisfying the "one container per task"
requirement while sharing dependencies and build cache.

The diagram is reproducible via `python hackathon/paper/architecture.py`
(requires `matplotlib`).

---

## 8. Configuration

Set via `.env` (loaded automatically; for secrets) or environment variables
(see `hackathon/config.py`). Defaults shown.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | — | Required. Primary LLM. |
| `GROQ_API_KEY` | — | Optional. Automatic fallback when Anthropic fails. |
| `HACKATHON_DATABASE_URL` | `postgresql+asyncpg://hackathon:hackathon@postgres:5432/hackathon` | Async Postgres DSN. |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant endpoint. |
| `HACKATHON_EMBEDDING_BACKEND` | `fastembed` | `fastembed` (default) or `pseudo` (smoke tests). |
| `HACKATHON_FASTEMBED_MODEL` | `BAAI/bge-small-en-v1.5` | 384-d sentence model. |
| `HACKATHON_YELP_DIR` | `/data/yelp` | Path inside the loader container. |
| `HACKATHON_YELP_HOST_DIR` | `~/datasets/yelp` | Host folder mounted by the Makefile. |
| `HACKATHON_MAX_YELP_USERS` | `5000` | Cap for loader sampling. |
| `HACKATHON_MAX_YELP_ITEMS` | `12000` | Cap for loader sampling. |
| `HACKATHON_HOLDOUT_FRACTION` | `0.1` | Per-user holdout for evaluation. |
| `HACKATHON_ALLOW_SYNTHETIC` | `1` | Fall back to generated data when no Yelp directory is mounted. |

---

## 9. Deployment (VPS)

The image is fully self-contained — `make hackathon-up` on the VPS is enough
once database state is present.

| Path | When to use |
|------|-------------|
| `docker compose build` on the VPS | Cleanest for one-off submissions; requires the source repository on the VPS. |
| `docker push entivia-hackathon` → `docker pull` on the VPS | Faster cold starts; no source required on the VPS. |

The full Yelp loader streams ~10 GB of JSON and CPU-embeds ~11 k items, which
is impractical on a small VPS. The recommended flow is to load locally and
ship the resulting state:

```bash
# locally, after `make hackathon-load` completes
docker run --rm -v hackathon_hackathon_pg:/v -v "$PWD":/out alpine \
  tar czf /out/hackathon_pg.tgz -C /v .
docker run --rm -v hackathon_hackathon_qdrant:/v -v "$PWD":/out alpine \
  tar czf /out/hackathon_qdrant.tgz -C /v .
# scp the two tarballs to the VPS, then restore them into the named volumes.
```

The nginx fragment used for production routing is in
`deploy/vps/nginx/pulse.conf` (`hackathon-a.entivia.online → :8011`,
`hackathon-b.entivia.online → :8012`).

---

## 10. Troubleshooting

| Symptom | Likely cause and fix |
|---------|----------------------|
| `502` on `/recommend` immediately after start | Wait for `make hackathon-load` to finish; the Qdrant collection is empty until then. |
| Anthropic API error in logs | Missing or invalid `ANTHROPIC_API_KEY`. Set `GROQ_API_KEY` for automatic fallback. |
| `/metrics` returns `available: false` | Run `make hackathon-eval` (or the `--skip-llm` variant) to produce `eval/data/EVAL.md`. |
| Hit@10 lower than expected | Confirm `make hackathon-load` ran successfully; Hit@K is measured only against held-out positives. |
| `GET /samples/users` returns `[]` | The loader has not run, or `MIN_REVIEWS_PER_USER` filtered every user out. |

For any failed request, inspect the per-response `meta` block — fields such
as `provider_fallbacks`, `validation_retries`, and `tool_failures` typically
identify the root cause.

---

## Contact

Submission contact details are in `SUBMISSION.md`. The runtime this project is
built on is the same one powering [entivia.online](https://entivia.online).
