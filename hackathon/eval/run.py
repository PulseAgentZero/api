"""End-to-end eval runner: Task A, Task B, and the baseline ablations.

Writes a Markdown report to ``hackathon/eval/data/EVAL.md`` so it survives
container restarts (the file lives on a Docker volume mount).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
from rouge_score import rouge_scorer

from hackathon.agents.recommender import RecommendationAgent
from hackathon.agents.review_simulator import ReviewSimulationAgent
from hackathon.core.embeddings import pseudo_embed
from hackathon.core.repository import (
    fetch_user_history,
    fetch_user_profile,
    get_user_vector,
)
from hackathon.core.vector_store import vector_store

logger = logging.getLogger(__name__)
HOLDOUT = Path(__file__).resolve().parent / "data" / "holdout_yelp.jsonl"
OUTPUT = Path(__file__).resolve().parent / "data" / "EVAL.md"


# ── Data + metric helpers ─────────────────────────────────────────────────────

def _load_holdout() -> list[dict]:
    with open(HOLDOUT, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ndcg_at_k(rels: Iterable[float], k: int = 10) -> float:
    rels = list(rels)[:k]
    dcg = sum((2**r - 1) / math.log2(i + 2) for i, r in enumerate(rels))
    ideal = sorted(rels, reverse=True)
    idcg = sum((2**r - 1) / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def _rmse(preds: list[int], truths: list[int]) -> float:
    if not preds:
        return float("nan")
    return float(np.sqrt(np.mean([(p - t) ** 2 for p, t in zip(preds, truths)])))


def _rouge_l(preds: list[str], truths: list[str]) -> float:
    scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)
    scores = [
        scorer.score(t, p)["rougeL"].fmeasure
        for p, t in zip(preds, truths)
        if t
    ]
    return float(np.mean(scores)) if scores else float("nan")


# ── Task A — Review simulation ────────────────────────────────────────────────

@dataclass
class TaskAResult:
    voice: str
    n: int
    rmse: float
    rouge_l: float
    bert_f1: float | None = None


async def task_a(sample: int, voice: str, *, use_bert: bool) -> TaskAResult:
    rows = _load_holdout()
    random.Random(7).shuffle(rows)
    rows = rows[:sample]

    agent = ReviewSimulationAgent()
    pred_stars, true_stars, pred_text, true_text = [], [], [], []
    sem = asyncio.Semaphore(8)

    async def one(row: dict) -> None:
        async with sem:
            try:
                out = await agent.simulate(
                    user_id=row["user_id"],
                    item_id=row["item_id"],
                    voice=voice,
                )
                pred_stars.append(out["stars"])
                true_stars.append(int(round(row["stars"])))
                pred_text.append(out["text"])
                true_text.append(row.get("text", ""))
            except Exception as exc:
                logger.warning("Task A skip user=%s: %s", row.get("user_id"), exc)

    await asyncio.gather(*(one(r) for r in rows))
    bert_f1 = _maybe_bert_score(pred_text, true_text) if use_bert else None
    return TaskAResult(
        voice=voice,
        n=len(pred_stars),
        rmse=_rmse(pred_stars, true_stars),
        rouge_l=_rouge_l(pred_text, true_text),
        bert_f1=bert_f1,
    )


def _maybe_bert_score(preds: list[str], truths: list[str]) -> float | None:
    if not preds:
        return None
    try:
        from bert_score import score as bert_score  # type: ignore[import-not-found]
    except ImportError:
        logger.info("bert_score not installed in the image — skipping (pip install bert-score torch)")
        return None
    try:
        _, _, f1 = bert_score(
            preds,
            truths,
            lang="en",
            model_type="distilbert-base-uncased",
            verbose=False,
        )
        return float(f1.mean())
    except Exception as exc:
        logger.warning("BERTScore failed at runtime: %s", exc)
        return None


# ── Task B — Recommendation ───────────────────────────────────────────────────

@dataclass
class TaskBResult:
    mode: str
    n: int
    hit_at_k: float
    ndcg_at_k: float | None
    k: int = 10


def _group_positives_by_user(rows: list[dict]) -> dict[str, list[dict]]:
    by_user: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if float(row.get("stars", 0)) >= 4:
            by_user[row["user_id"]].append(row)
    return by_user


async def task_b_warm(sample_users: int, k: int, *, use_llm: bool) -> TaskBResult:
    by_user = _group_positives_by_user(_load_holdout())
    users = list(by_user)
    random.Random(11).shuffle(users)
    users = users[:sample_users]

    agent = RecommendationAgent() if use_llm else None
    hits: list[float] = []
    ndcgs: list[float] = []
    sem = asyncio.Semaphore(6)

    async def one(uid: str) -> None:
        async with sem:
            positives = {r["item_id"]: float(r["stars"]) for r in by_user[uid]}
            try:
                ranked = await _rank_for_user(uid, k, agent)
                rels = [max(0.0, positives.get(iid, 0) - 3) for iid in ranked]
                hits.append(1.0 if any(iid in positives for iid in ranked) else 0.0)
                ndcgs.append(_ndcg_at_k(rels, k))
            except Exception as exc:
                logger.warning("Task B skip user=%s: %s", uid, exc)

    await asyncio.gather(*(one(u) for u in users))
    return TaskBResult(
        mode="ann+llm" if use_llm else "ann-only",
        n=len(hits),
        hit_at_k=sum(hits) / len(hits) if hits else 0.0,
        ndcg_at_k=(sum(ndcgs) / len(ndcgs)) if ndcgs else 0.0,
        k=k,
    )


async def _rank_for_user(
    user_id: str,
    k: int,
    agent: RecommendationAgent | None,
) -> list[str]:
    if agent:
        out = await agent.recommend(user_id=user_id, k=k, dataset="yelp")
        return [it.get("item_id") for it in out.get("items", []) if it.get("item_id")]

    vector = await get_user_vector(user_id) or pseudo_embed(user_id)
    history = await fetch_user_history(user_id, limit=200)
    exclude = [h["item_id"] for h in history]
    candidates = await vector_store.search(
        vector, k=k, dataset="yelp", exclude_item_ids=exclude
    )
    return [c["item_id"] for c in candidates]


async def task_b_cold(sample: int, k: int) -> TaskBResult:
    rows = _load_holdout()
    random.Random(13).shuffle(rows)
    rows = rows[:sample]
    agent = RecommendationAgent()
    hits: list[float] = []

    async def one(row: dict) -> None:
        persona = f"enjoys {row.get('text', '')[:120]}"
        try:
            out = await agent.recommend(persona=persona, k=k, dataset="yelp")
            ranked = {it.get("item_id") for it in out.get("items", [])}
            hits.append(1.0 if row["item_id"] in ranked else 0.0)
        except Exception as exc:
            logger.warning("Task B cold skip: %s", exc)

    await asyncio.gather(*(one(r) for r in rows))
    return TaskBResult(
        mode="cold-persona",
        n=len(hits),
        hit_at_k=(sum(hits) / len(hits)) if hits else 0.0,
        ndcg_at_k=None,
        k=k,
    )


# ── Baselines ─────────────────────────────────────────────────────────────────

@dataclass
class BaselineResult:
    mode: str
    n: int
    rmse: float


async def profile_baseline_rmse(sample: int) -> BaselineResult:
    """No-LLM ablation: predict each user's average historical rating."""
    rows = _load_holdout()
    random.Random(9).shuffle(rows)
    rows = rows[:sample]
    errors: list[float] = []
    for row in rows:
        profile = await fetch_user_profile(row["user_id"])
        if not profile or profile.get("avg_stars") is None:
            continue
        pred = int(round(float(profile["avg_stars"])))
        truth = int(round(row["stars"]))
        errors.append((pred - truth) ** 2)
    return BaselineResult(
        mode="avg-stars-baseline",
        n=len(errors),
        rmse=float(np.sqrt(np.mean(errors))) if errors else float("nan"),
    )


# ── Report writer ─────────────────────────────────────────────────────────────

@dataclass
class EvalReport:
    task_a: list[TaskAResult] = field(default_factory=list)
    task_b: list[TaskBResult] = field(default_factory=list)
    baselines: list[BaselineResult] = field(default_factory=list)
    note: str = ""

    def render(self) -> str:
        lines: list[str] = [
            "# Hackathon evaluation results\n",
            f"\n{self.note}\n",
            "\n## Task A — Review simulation (agent)\n",
            "| Voice | N | RMSE ↓ | ROUGE-L ↑ | BERTScore F1 ↑ |\n",
            "|---|---:|---:|---:|---:|\n",
        ]
        for r in self.task_a:
            bert = f"{r.bert_f1:.3f}" if r.bert_f1 is not None else "—"
            lines.append(
                f"| {r.voice} | {r.n} | {r.rmse:.3f} | {r.rouge_l:.3f} | {bert} |\n"
            )

        lines.append("\n## Task A — Baselines\n")
        lines.append("| Mode | N | RMSE |\n|---|---:|---:|\n")
        for b in self.baselines:
            lines.append(f"| {b.mode} | {b.n} | {b.rmse:.3f} |\n")

        lines.append("\n## Task B — Recommendation\n")
        lines.append("| Mode | Users/N | K | Hit@K ↑ | NDCG@K ↑ |\n|---|---:|---:|---:|---:|\n")
        for r in self.task_b:
            ndcg = f"{r.ndcg_at_k:.3f}" if r.ndcg_at_k is not None else "—"
            lines.append(f"| {r.mode} | {r.n} | {r.k} | {r.hit_at_k:.3f} | {ndcg} |\n")
        return "".join(lines)

    def write(self, path: Path) -> None:
        path.write_text(self.render(), encoding="utf-8")
        logger.info("Wrote %s", path)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument("--task-a-sample", type=int, default=30)
    parser.add_argument("--task-b-users", type=int, default=30)
    parser.add_argument("--nigerian-sample", type=int, default=15)
    parser.add_argument("--cold-sample", type=int, default=20)
    parser.add_argument("--bert", action="store_true", help="Add BERTScore F1 to Task A")
    parser.add_argument("--skip-llm", action="store_true", help="ANN + baseline only")
    return parser.parse_args()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    args = _parse_args()

    report = EvalReport(
        note=(
            "Metrics on Yelp holdout (`eval/data/holdout_yelp.jsonl`). "
            "Re-run with `ANTHROPIC_API_KEY` (and optional `GROQ_API_KEY` fallback) "
            "for full agent scores."
        )
    )

    report.baselines.append(
        await profile_baseline_rmse(min(200, args.task_a_sample * 3))
    )
    report.task_b.append(await task_b_warm(args.task_b_users, k=10, use_llm=False))

    if not args.skip_llm:
        report.task_a.append(await task_a(args.task_a_sample, "default", use_bert=args.bert))
        if args.nigerian_sample:
            report.task_a.append(
                await task_a(args.nigerian_sample, "nigerian", use_bert=False)
            )
        report.task_b.append(await task_b_warm(args.task_b_users, k=10, use_llm=True))
        report.task_b.append(await task_b_cold(min(args.cold_sample, args.task_b_users), k=10))

    report.write(OUTPUT)
    print(f"Done: {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
