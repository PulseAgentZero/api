"""Deterministic synthetic Yelp + Goodreads slice for offline demos.

Used when the real Yelp dump is not mounted into the container. Generates
enough variety to exercise the full agent pipeline (warm-start, cold-start,
cross-domain) without external downloads.
"""

from __future__ import annotations

import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hackathon.config import MAX_YELP_USERS, MIN_REVIEWS_PER_USER
from hackathon.core import db as hack_db

_RNG = random.Random(42)

_CATEGORIES = (
    "Restaurants",
    "Nigerian",
    "Fast Food",
    "Cafes",
    "Bars",
    "Seafood",
    "Pizza",
    "Chinese",
    "Indian",
    "Bakeries",
)
_REVIEW_SNIPPETS = (
    "Solid food and friendly staff. Would come back.",
    "Portions were generous but service was slow.",
    "Great ambiance for a date night.",
    "Too noisy during peak hours.",
    "The jollof rice was decent sha, not the best in Lagos though.",
    "Place dey too crowded but the suya still slaps.",
    "Clean space, fair prices, nothing extraordinary.",
    "Best pepper soup I've had in a while.",
    "Parking was a nightmare.",
    "Quick lunch spot — in and out under 30 minutes.",
)
_BIZ_NAMES = (
    "Mama Put Kitchen",
    "Lagos Grill House",
    "Island Bites",
    "Suya Spot VI",
    "Pepper & Palm",
    "The Shawarma Lane",
    "Coastal Catch",
    "Mainland Munchies",
    "Urban Roast Cafe",
    "Night Market Eatery",
)
_STAR_WEIGHTS = (2, 5, 15, 35, 43)  # skews positive like real Yelp distributions
_GOODREADS_TITLES = (
    "Things Fall Apart",
    "Americanah",
    "Purple Hibiscus",
    "Half of a Yellow Sun",
    "The Famished Road",
    "Stay With Me",
    "Welcome to Lagos",
    "My Sister the Serial Killer",
)
_GOODREADS_AUTHORS = ("Chinua Achebe", "Chimamanda Ngozi Adichie", "Ben Okri")
_GOODREADS_GENRES = ("Fiction", "Africa", "Literary", "Historical")


def _build_items(n_items: int) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for i in range(n_items):
        category = _RNG.choice(_CATEGORIES)
        items[f"yelp_biz_{i}"] = {
            "name": f"{_RNG.choice(_BIZ_NAMES)} #{i}",
            "metadata": {
                "categories": category,
                "city": _RNG.choice(("Lagos", "Abuja", "Port Harcourt", "Ibadan")),
                "stars": round(_RNG.uniform(2.5, 4.8), 1),
            },
        }
    return items


async def seed_synthetic_yelp(
    session: AsyncSession, n_users: int | None = None
) -> dict[str, Any]:
    n_users = n_users or min(800, MAX_YELP_USERS)
    items = _build_items(max(200, n_users * 3))
    for iid, meta in items.items():
        await hack_db.upsert_item(session, iid, "yelp", meta["name"], meta["metadata"])

    holdout_rows: list[dict[str, Any]] = []
    review_count = 0
    base_ts = datetime.now(timezone.utc) - timedelta(days=900)

    for user_idx in range(n_users):
        uid = f"yelp_user_{user_idx}"
        n_reviews = _RNG.randint(MIN_REVIEWS_PER_USER, MIN_REVIEWS_PER_USER + 8)
        chosen = _RNG.sample(list(items), min(n_reviews, len(items)))

        # FK requires the user to exist before its reviews — persona meta is rewritten below.
        await hack_db.upsert_user(session, uid, "yelp", {"n_reviews": n_reviews})

        stars_list: list[float] = []
        cat_counter: Counter[str] = Counter()

        for review_idx, iid in enumerate(chosen):
            stars = float(_RNG.choices([1, 2, 3, 4, 5], weights=_STAR_WEIGHTS)[0])
            stars_list.append(stars)
            cat_counter[items[iid]["metadata"]["categories"]] += 1
            is_holdout = (review_idx == len(chosen) - 1) and len(chosen) > 2
            text_body = _RNG.choice(_REVIEW_SNIPPETS)
            await hack_db.insert_review(
                session,
                f"rev_{uuid.uuid4().hex[:12]}",
                uid,
                iid,
                "yelp",
                stars,
                text_body,
                base_ts + timedelta(days=_RNG.randint(0, 800)),
                is_holdout,
            )
            review_count += 1
            if is_holdout:
                holdout_rows.append(
                    {"user_id": uid, "item_id": iid, "stars": stars, "text": text_body}
                )

        await hack_db.upsert_user(
            session,
            uid,
            "yelp",
            {
                "avg_stars": round(sum(stars_list) / len(stars_list), 2),
                "n_reviews": len(stars_list),
                "top_categories": [c for c, _ in cat_counter.most_common(3)],
            },
        )

    return {
        "users": n_users,
        "items": len(items),
        "reviews": review_count,
        "holdout": holdout_rows,
    }


async def seed_synthetic_goodreads(session: AsyncSession, n: int = 1000) -> int:
    for i in range(n):
        await hack_db.upsert_item(
            session,
            f"gr_book_{i}",
            "goodreads",
            f"{_RNG.choice(_GOODREADS_TITLES)} — Vol {i % 50}",
            {
                "authors": [_RNG.choice(_GOODREADS_AUTHORS)],
                "genres": _RNG.sample(_GOODREADS_GENRES, k=2),
            },
        )
    return n
