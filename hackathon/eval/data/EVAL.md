# Hackathon evaluation results

Metrics on Yelp holdout (`eval/data/holdout_yelp.jsonl`). Re-run with `ANTHROPIC_API_KEY` (and optional `GROQ_API_KEY` fallback) for full agent scores.

## Task A — Review simulation (agent)
| Voice | N | RMSE ↓ | ROUGE-L ↑ | BERTScore F1 ↑ |
|---|---:|---:|---:|---:|
| default | 30 | 0.931 | 0.144 | — |
| nigerian | 15 | 1.000 | 0.130 | — |

## Task A — Baselines
| Mode | N | RMSE |
|---|---:|---:|
| avg-stars-baseline | 90 | 1.216 |

## Task B — Recommendation
| Mode | Users/N | K | Hit@K ↑ | NDCG@K ↑ |
|---|---:|---:|---:|---:|
| ann-only | 0 | 10 | 0.000 | 0.000 |
| ann+llm | 0 | 10 | 0.000 | 0.000 |
| cold-persona | 0 | 10 | 0.000 | — |
