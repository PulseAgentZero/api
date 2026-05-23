"""Stream-load the Yelp Open Dataset (JSON Lines) into Postgres.

Designed for the academic dump (`yelp_academic_dataset_*.json`) but also works
with the shorter file names some redistributions use. Memory footprint stays
bounded because reviews are streamed twice and only the eligible subset is
held in RAM.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from hackathon.config import (
    HACKATHON_YELP_DIR,
    HOLDOUT_FRACTION,
    MAX_YELP_ITEMS,
    MAX_YELP_USERS,
    MIN_REVIEWS_PER_USER,
)
from hackathon.core import db as hack_db

logger = logging.getLogger(__name__)

_FOOD_KEYWORDS = (
    "restaurant",
    "food",
    "cafe",
    "coffee",
    "bar",
    "nightlife",
    "bakery",
    "dessert",
)
_FILE_VARIANTS = (
    ("review.json", "business.json", "user.json"),
    (
        "yelp_academic_dataset_review.json",
        "yelp_academic_dataset_business.json",
        "yelp_academic_dataset_user.json",
    ),
)


@dataclass
class YelpStats:
    users: int
    items: int
    reviews: int
    holdout: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "users": self.users,
            "items": self.items,
            "reviews": self.reviews,
            "holdout": self.holdout,
        }


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with open(path, "rb") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_food_business(categories: str | None) -> bool:
    cats = (categories or "").lower()
    return any(keyword in cats for keyword in _FOOD_KEYWORDS)


def _resolve_paths(base: Path) -> tuple[Path, Path, Path]:
    for review, business, user in _FILE_VARIANTS:
        review_p, business_p, user_p = base / review, base / business, base / user
        if review_p.is_file() and business_p.is_file():
            return review_p, business_p, (user_p if user_p.is_file() else business_p)
    raise FileNotFoundError(f"Yelp JSON files not found under {base}")


def _load_businesses(path: Path) -> dict[str, dict[str, Any]]:
    businesses: dict[str, dict[str, Any]] = {}
    for row in _iter_jsonl(path):
        bid = row.get("business_id") or row.get("bid")
        if not bid:
            continue
        if not _is_food_business(row.get("categories")):
            continue
        businesses[bid] = {
            "name": row.get("name", bid),
            "metadata": {
                "categories": row.get("categories", ""),
                "city": row.get("city", ""),
                "state": row.get("state", ""),
                "stars": row.get("stars"),
            },
        }
        if len(businesses) >= MAX_YELP_ITEMS:
            break
    return businesses


def _select_eligible_users(
    review_path: Path, businesses: dict[str, dict[str, Any]]
) -> set[str]:
    counts: dict[str, int] = defaultdict(int)
    for row in _iter_jsonl(review_path):
        if row.get("user_id") and row.get("business_id") in businesses:
            counts[row["user_id"]] += 1
    eligible = [u for u, c in counts.items() if c >= MIN_REVIEWS_PER_USER][:MAX_YELP_USERS]
    return set(eligible)


def _collect_user_reviews(
    review_path: Path,
    businesses: dict[str, dict[str, Any]],
    eligible: set[str],
) -> dict[str, list[dict[str, Any]]]:
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _iter_jsonl(review_path):
        if row.get("user_id") in eligible and row.get("business_id") in businesses:
            by_user[row["user_id"]].append(row)
    return by_user


def _build_persona(
    reviews: list[dict[str, Any]],
    businesses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    stars = [float(r.get("stars", 3)) for r in reviews]
    cat_counter: dict[str, int] = defaultdict(int)
    for r in reviews:
        biz = businesses.get(r.get("business_id"))
        if not biz:
            continue
        for cat in str(biz["metadata"].get("categories", "")).split(","):
            cat = cat.strip()
            if cat:
                cat_counter[cat] += 1
    return {
        "avg_stars": round(sum(stars) / len(stars), 2),
        "n_reviews": len(reviews),
        "top_categories": sorted(cat_counter, key=cat_counter.get, reverse=True)[:3],
    }


async def _persist_review_batch(
    session: AsyncSession,
    user_id: str,
    reviews: list[dict[str, Any]],
    businesses: dict[str, dict[str, Any]],
    items_seen: set[str],
    holdout_rows: list[dict[str, Any]],
    is_holdout: bool,
) -> int:
    written = 0
    for row in reviews:
        bid = row["business_id"]
        biz = businesses.get(bid)
        if not biz:
            continue
        if bid not in items_seen:
            await hack_db.upsert_item(session, bid, "yelp", biz["name"], biz["metadata"])
            items_seen.add(bid)
        review_id = row.get("review_id") or f"{user_id}_{bid}_{row.get('date', '')}"
        stars = float(row.get("stars", 3))
        text_body = row.get("text") or ""
        await hack_db.insert_review(
            session,
            review_id,
            user_id,
            bid,
            "yelp",
            stars,
            text_body,
            _parse_date(row.get("date")),
            is_holdout,
        )
        written += 1
        if is_holdout:
            holdout_rows.append(
                {
                    "user_id": user_id,
                    "item_id": bid,
                    "stars": stars,
                    "text": text_body,
                }
            )
    return written


async def load_yelp(
    session: AsyncSession, base_dir: Path | None = None
) -> dict[str, Any]:
    base = base_dir or HACKATHON_YELP_DIR
    if not base or not base.is_dir():
        raise FileNotFoundError("HACKATHON_YELP_DIR not set or missing")

    review_path, business_path, _ = _resolve_paths(base)
    logger.info("Loading businesses from %s", business_path)
    businesses = _load_businesses(business_path)

    logger.info("Pass 1: counting reviews per user from %s", review_path)
    eligible = _select_eligible_users(review_path, businesses)
    logger.info(
        "Selected %d users with >= %d reviews",
        len(eligible),
        MIN_REVIEWS_PER_USER,
    )

    by_user = _collect_user_reviews(review_path, businesses, eligible)

    stats = YelpStats(users=len(eligible), items=0, reviews=0)
    items_seen: set[str] = set()

    for user_id, reviews in by_user.items():
        reviews.sort(key=lambda r: r.get("date", ""))
        n_holdout = max(1, int(len(reviews) * HOLDOUT_FRACTION))
        train, test = reviews[:-n_holdout], reviews[-n_holdout:]

        persona = _build_persona(reviews, businesses)
        await hack_db.upsert_user(session, user_id, "yelp", persona)

        stats.reviews += await _persist_review_batch(
            session, user_id, train, businesses, items_seen, stats.holdout, False
        )
        stats.reviews += await _persist_review_batch(
            session, user_id, test, businesses, items_seen, stats.holdout, True
        )

    stats.items = len(items_seen)
    return stats.to_dict()
