# Hackathon evaluation results

Metrics on Yelp holdout (`eval/data/holdout_yelp.jsonl`). Re-run with `ANTHROPIC_API_KEY` (and optional `GROQ_API_KEY` fallback) for full agent scores.

## Task A — Review simulation (agent)
| Voice | N | RMSE ↓ | ROUGE-L ↑ | BERTScore F1 ↑ |
|---|---:|---:|---:|---:|
| default | 30 | 0.913 | 0.152 | — |
| nigerian | 15 | 0.894 | 0.129 | — |

## Task A — Baselines
| Mode | N | RMSE |
|---|---:|---:|
| avg-stars-baseline | 90 | 1.216 |

## Task B — Recommendation
| Mode | Users/N | K | Hit@K ↑ | NDCG@K ↑ |
|---|---:|---:|---:|---:|
| ann-only | 30 | 10 | 0.033 | 0.017 |
| ann+llm | 30 | 10 | 0.033 | 0.033 |
| cold-persona | 20 | 10 | 0.250 | — |
