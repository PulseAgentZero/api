# DSN × Bluechip LLM Agent Challenge — submission checklist

| Field | Value |
|-------|-------|
| **Task A API (Swagger)** | https://hackathon-a.entivia.online/docs — local: http://localhost:8011/docs |
| **Task B API (Swagger)** | https://hackathon-b.entivia.online/docs — local: http://localhost:8012/docs |
| **Combined gateway (optional)** | http://localhost:8010/docs |
| **Platform context** | https://entivia.online |
| **Repository** | GitHub monorepo — `hackathon/` (challenge code) + `app/` (Entivia engine) |
| **Solution papers (PDF)** | [`paper/task_a_review_simulation.pdf`](paper/task_a_review_simulation.pdf), [`paper/task_b_recommendation.pdf`](paper/task_b_recommendation.pdf) — generated from the `.md` sources via `make hackathon-paper-pdf` |
| **Eval metrics** | [`eval/data/EVAL.md`](eval/data/EVAL.md) — `GET /metrics` on either task container |

## Artifacts

1. **Two containerized apps** (same image, separate services in `docker-compose.yml`):
   - `task-a-api` — `POST /simulate-review` (persona + product **or** user_id + item_id)
   - `task-b-api` — `POST /recommend` (warm / cold / multi-turn / cross-domain)
2. **Code** — both agents are implemented in `app/agents/workflows/` (`review_simulator.py`, `cold_start_recommender.py`) on top of `app.agents.base.BaseAgent`; prompts live at `app/agents/workflows/prompts/simulation/`; `hackathon/agents/*` are thin shims that wire DB-mode tools.
3. **Two solution papers** — one per task (`make hackathon-paper-a-pdf`, `make hackathon-paper-b-pdf`).
4. **Evaluation** — RMSE, ROUGE-L (Task A); Hit@10, NDCG@10 (Task B); baselines in `GET /metrics`.
5. **Same agents on the live platform** — the dashboard's API Playground exposes a *Simulation* group calling `POST /api/public/v1/simulation/{review,recommend}` (direct / cold-start modes only) against the same agent code, under `X-API-Key` auth and the standard public-API rate limits.

## Local reproduction

```bash
cp hackathon/.env.example hackathon/.env   # HACKATHON_DATABASE_PASSWORD + ANTHROPIC_API_KEY
make hackathon-up                   # postgres + qdrant + task-a + task-b + gateway

# Point at the Yelp Open Dataset folder containing the four
# `yelp_academic_dataset_*.json` files. data/yelp_dataset/ also works.
HACKATHON_YELP_HOST_DIR=$PWD/data/yelp_dataset make hackathon-load
make hackathon-eval                 # writes hackathon/eval/data/EVAL.md

open http://localhost:8011/docs     # Task A
open http://localhost:8012/docs     # Task B
```

Submitted run loaded a 5,000-user / 11,397-item / 112,157-review food slice
of real Yelp (~9,447 holdout rows). Agent beats the avg-stars baseline by
23 % on RMSE (0.894 vs 1.164); cold-start persona Hit@10 = 0.350 (~400× random
on an 11,397-item catalogue). The warm-start retrieval row sits at 0 on this
holdout — see Task B paper §7.3 for the geometric explanation.

**Task A direct input (challenge spec):**

```bash
curl -s http://localhost:8011/simulate-review -H 'Content-Type: application/json' -d '{
  "persona": {
    "description": "Generous reviewer who loves spicy Nigerian food.",
    "avg_stars": 4.2,
    "top_categories": ["Nigerian", "Restaurants"]
  },
  "product": {
    "name": "Tam Tam African Restaurant",
    "categories": "African, Restaurants",
    "city": "Philadelphia"
  }
}'
```

## VPS deployment

```bash
make hackathon-up
# Restore pre-loaded postgres + qdrant volumes (see README), then:
docker compose -f hackathon/docker-compose.yml restart task-a-api task-b-api
```

nginx: proxy `hackathon-a` → `:8011`, `hackathon-b` → `:8012` (see `deploy/vps/nginx/pulse.conf`).
