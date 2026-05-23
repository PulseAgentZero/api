# Hackathon evaluation results

Metrics on Yelp holdout (`eval/data/holdout_yelp.jsonl`). Re-run with `ANTHROPIC_API_KEY` (and optional `GROQ_API_KEY` fallback) for full agent scores.

## Task A — Review simulation (agent)
| Voice | N | RMSE ↓ | ROUGE-L ↑ | BERTScore F1 ↑ |
|---|---:|---:|---:|---:|

## Task A — Baselines
| Mode | N | RMSE |
|---|---:|---:|
| avg stars (no LLM) | 90 | 1.216 |

## Task B — Recommendation
| Mode | Users/N | K | Hit@K ↑ | NDCG@K ↑ |
|---|---:|---:|---:|---:|
| ann-only | 100 | 10 | 0.010 | 0.005 |
