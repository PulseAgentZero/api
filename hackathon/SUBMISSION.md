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
cp .env.example .env                # ANTHROPIC_API_KEY (+ optional GROQ_API_KEY)
make hackathon-up                   # postgres + qdrant + task-a + task-b + gateway
HACKATHON_YELP_HOST_DIR=~/datasets/yelp make hackathon-load
open http://localhost:8011/docs     # Task A
open http://localhost:8012/docs     # Task B
```

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
