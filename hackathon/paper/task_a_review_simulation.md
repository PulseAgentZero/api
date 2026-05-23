# Task A — Review Simulation Agent

**DSN × Bluechip Technologies LLM Agent Challenge 3.0**  
Team Entivia — May 2026  
Live API: `http://localhost:8011/docs` (container `task-a-api`)  
Platform context: [entivia.online](https://entivia.online)

---

## Abstract

We submit a containerized **Review Simulation Agent** that accepts a **user persona** and **product details** as input and returns a predicted **star rating (1–5)** and **review text**. The agent is built on Entivia's production `BaseAgent` ReAct runtime (Anthropic Claude with Groq fallback, JSON validation, tool calling). We evaluate on a Yelp restaurant slice with per-user holdout reviews, reporting **RMSE**, **ROUGE-L**, and optional **BERTScore**, plus a **Nigerian English** voice variant for the localization bonus.

---

## 1. Problem

Review platforms encode behavioral signals: rating generosity, category preferences, and linguistic style. The challenge asks for an agent that simulates how a specific user would review an unseen product — not a generic LLM paragraph.

Key difficulties:
- Grounding in persona facts (avoid hallucinated preferences)
- Calibrating star ratings to historical tendency
- Matching writing style in free text

---

## 2. Approach

### Input contract (challenge spec)

```json
{
  "persona": {
    "description": "Generous reviewer who loves spicy Nigerian food and cafes.",
    "avg_stars": 4.2,
    "top_categories": ["Nigerian", "Restaurants"],
    "sample_reviews": ["The jollof was fire — generous portions."]
  },
  "product": {
    "name": "Tam Tam African Restaurant",
    "categories": "African, Restaurants",
    "city": "Philadelphia",
    "stars": 3.5
  },
  "voice": "default"
}
```

**Output:** `{"stars": 4, "text": "..."}` plus runtime `meta` (model, tokens, latency).

### Agent design — `ReviewSimulationAgent`

Extends `app.agents.base.BaseAgent`:

| Mode | When | Behavior |
|------|------|----------|
| **Direct** | `persona` + `product` in request | Persona and product inlined in prompt; no DB tools |
| **DB demo** | `user_id` + `item_id` | Tools: `fetch_user_profile`, `fetch_item` |

Steps:
1. Load persona (inline or via `fetch_user_profile`)
2. Load product (inline or via `fetch_item`)
3. LLM generates JSON `{stars, text}` validated by `JsonValidator` with retry (`tenacity`)

### Nigerian voice

`voice=nigerian` switches system prompt to Nigerian English / light Pidgin tone while keeping the same grounding rules — demonstrates controlled style transfer without a separate model.

---

## 3. Architecture

![End-to-end architecture (Task A highlighted, left)](architecture.png)

```text
POST /simulate-review
        │
        ▼
ReviewSimulationAgent (BaseAgent ReAct)
        │
   ┌────┴────┐
   │ Direct  │  persona + product JSON in prompt
   │ DB mode │  Postgres tools → users / items / reviews
   └────┬────┘
        ▼
   Claude (+ Groq fallback) → {stars, text} + meta
```

**Container:** `task-a-api` — dedicated Docker service on port **8011**, same image as Task B but isolated process per hackathon requirement.

**Data (DB demo mode):** Yelp Open Dataset — 5k users (≥10 reviews), restaurant/food businesses, 10% per-user holdout for evaluation. Embeddings not required for Task A inference.

---

## 4. Experiments

| Metric | Meaning |
|--------|---------|
| RMSE ↓ | Star rating error vs held-out review |
| ROUGE-L ↑ | Lexical overlap of generated vs real text |
| BERTScore F1 ↑ | Semantic similarity (optional) |

**Baseline:** predict user's historical `avg_stars` (no LLM) — RMSE ~1.22 on 90-sample holdout.

**Agent eval:** `python -m hackathon.eval.run --task-a-sample 30` (requires `ANTHROPIC_API_KEY`). Results exposed via `GET /metrics`.

**Ablations:**
- Default vs Nigerian voice
- Direct persona input vs DB-backed `user_id`/`item_id`

---

## 5. Why agentic vs template?

Tool-backed DB mode mirrors how Entivia grounds enterprise agents in live SQL — every claim about the user can be traced to stored reviews. Direct mode satisfies the hackathon input spec without requiring judges to know internal user ids.

---

## 6. Limitations & future work

- Full 300-sample eval is API-cost/latency bounded; run before final submission.
- BERTScore omitted from Docker image size constraints; available on host.
- Future: fine-tuned star head, style adapters per locale, streaming eval harness.

---

## References

- Entivia `BaseAgent`: `app/agents/base.py`
- Hackathon agent: `hackathon/agents/review_simulator.py`
- Yelp Open Dataset
