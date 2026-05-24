# Hackathon evaluation results

Metrics on Yelp holdout (`eval/data/holdout_yelp.jsonl`). Re-run with `ANTHROPIC_API_KEY` (and optional `GROQ_API_KEY` fallback) for full agent scores.

## Task A — Review simulation (agent)
| Voice | N | RMSE ↓ | ROUGE-L ↑ | BERTScore F1 ↑ |
|---|---:|---:|---:|---:|
| default | 60 | 0.894 | 0.139 | — |
| nigerian | 30 | 0.983 | 0.130 | — |

## Task A — Baselines
| Mode | N | RMSE |
|---|---:|---:|
| avg-stars-baseline | 180 | 1.164 |

## Task B — Recommendation
| Mode | Users/N | K | Hit@K ↑ | NDCG@K ↑ |
|---|---:|---:|---:|---:|
| ann-only | 99 | 10 | 0.000 | 0.000 |
| ann+llm | 97 | 10 | 0.000 | 0.000 |
| cold-persona | 40 | 10 | 0.350 | — |
