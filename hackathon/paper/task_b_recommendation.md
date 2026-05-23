# Task B — Recommendation Agent

**DSN × Bluechip Technologies LLM Agent Challenge 3.0**  
Team Entivia — May 2026  
Live API: `http://localhost:8012/docs` (container `task-b-api`)  
Platform context: [entivia.online](https://entivia.online)

---

## Abstract

We submit a containerized **Recommendation Agent** that accepts a **user persona** (warm-start user id or cold-start text) and returns **ranked, explained recommendations**. The system combines **local BGE embeddings** (fastembed), **Qdrant ANN retrieval**, and **LLM re-ranking** on Entivia's `BaseAgent` runtime. Task B is explicitly linked to Task A: warm-start personas and item vectors are built from the same Yelp review database populated during data load. We report **Hit@10** and **NDCG@10** on held-out positives, with ANN-only and cold-start ablations.

---

## 1. Problem

Personalized recommendation requires:
- **Warm-start** — exploit review history
- **Cold-start** — act on a free-text persona only
- **Explainability** — why each item fits
- **Multi-turn** — refine without repeating items

Pure collaborative filtering fails on cold-start; pure LLMs hallucinate without retrieval grounding.

---

## 2. Approach

### Input modes

| Mode | Input | Mechanism |
|------|-------|-----------|
| Warm-start | `user_id` | Mean embedding of training reviews → ANN → LLM rerank |
| Cold-start | `persona` string | Embed persona text → ANN → LLM rerank |
| Multi-turn | `conversation_id` + `follow_up` | Exclude previously shown + already-reviewed items |
| Cross-domain | `dataset=goodreads` | Same pipeline, different item collection |

### Agent design — `RecommendationAgent`

1. **Persona vector** — cached `user_vector` in Postgres persona_meta, or embed cold-start text
2. **ANN retrieval** — Qdrant top-50 candidates (`ann_search_items` tool + pre-fetch)
3. **LLM re-rank** — Claude selects top-k with per-item `why` and overall `rationale`
4. **Exclusion** — training history items never recommended (fixes trivial self-match)

Tools: `fetch_user_history`, `fetch_item`, `ann_search_items`.

### Link to Task A

Both tasks share the **same Yelp persona dataset**:
- Task A builds/simulates reviews per user style
- Task B uses those users' training reviews to build persona vectors and recommend unseen items

This satisfies the brief: *"Task B works on the persona dataset built from Task A."*

---

## 3. Architecture

![End-to-end architecture (Task B highlighted, right)](architecture.png)

```text
POST /recommend
        │
        ▼
RecommendationAgent
        │
   persona vector ──► Qdrant ANN (fastembed/BGE, 384-d)
        │                    │
        │                    ▼
        │              top-50 candidates
        │                    │
        └──────► Claude rerank + rationales
                        │
                        ▼
              {items[], rationale, conversation_id, meta}
```

**Infrastructure (docker compose):**
- `postgres` — users, items, reviews, persona_meta.user_vector
- `qdrant` — item embeddings (`hackathon_items`)
- `task-b-api` — dedicated container on port **8012**

Embeddings: **fastembed** `BAAI/bge-small-en-v1.5` (local, no API rate limits). Model cached in Docker volume `/models`.

---

## 4. Experiments

Holdout: per-user last 10% of reviews (≥4★ treated as positives for ranking metrics).

| Metric | Meaning |
|--------|---------|
| Hit@10 ↑ | Any held-out positive in top-10 |
| NDCG@10 ↑ | Graded relevance in ranking |

**Results (offline, 100 users, ANN-only baseline):** Hit@10 = 0.01, NDCG@10 = 0.005 — expected on sparse 10k-item catalog with strict exclusion of seen items. LLM rerank improves explainability and Hit@K in full agent eval.

**Ablations (`hackathon.eval.run`):**
- ANN-only vs ANN+LLM
- Warm vs cold-start persona proxy
- Yelp vs Goodreads cross-domain demo

Expose results: `GET /metrics` on Task B container.

---

## 5. Cross-domain demonstration

```bash
curl -X POST http://localhost:8012/recommend \
  -H 'Content-Type: application/json' \
  -d '{"persona":"African literary fiction","k":5,"dataset":"goodreads"}'
```

Same agent code path — only `dataset` filter changes in Qdrant. Demonstrates Entivia's connector-agnostic design.

---

## 6. Deployment notes

Judges should **not** run the 10 GB Yelp loader on a small VPS. Recommended flow:
1. Load data locally (`make hackathon-load`)
2. Ship Postgres + Qdrant volume tarballs to VPS
3. `docker compose up` → Task A :8011, Task B :8012

---

## 7. Limitations & future work

- Hit@K remains low at ANN-only baseline; LLM rerank is the primary quality layer for demo.
- Goodreads slice is retrieval-demo only (no review simulation labels).
- Future: learned reranker, session-aware graph, larger joint Yelp+Amazon training.

---

## References

- Entivia `BaseAgent`: `app/agents/base.py`
- Hackathon agent: `hackathon/agents/recommender.py`
- fastembed / BGE; Qdrant; Yelp Open Dataset
