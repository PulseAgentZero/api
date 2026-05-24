"""Read-side queries used by the hackathon agents and eval scripts.

All functions are async, open their own session, and never leak ORM rows out of
the function — the agents work with plain dicts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from hackathon.core.db import get_session

_RECENT_SAMPLE_REVIEWS = 5
_TEXT_SNIPPET_LIMIT = 400
_HISTORY_TEXT_LIMIT = 300


async def fetch_user_profile(user_id: str) -> dict[str, Any] | None:
    """Return persona meta + a handful of recent training reviews.

    The dict is shaped for direct LLM consumption (no SQLAlchemy types).
    """
    async with get_session() as session:
        user_row = (
            await session.execute(
                text("SELECT id, dataset, persona_meta FROM users WHERE id = :id"),
                {"id": user_id},
            )
        ).first()
        if not user_row:
            return None

        meta = dict(user_row.persona_meta or {})
        samples = await session.execute(
            text(
                """
                SELECT r.stars, r.text, i.name
                FROM reviews r
                JOIN items i ON i.id = r.item_id
                WHERE r.user_id = :uid AND r.is_holdout = FALSE
                ORDER BY r.reviewed_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": _RECENT_SAMPLE_REVIEWS},
        )
        sample_reviews = [
            {
                "stars": row.stars,
                "text": (row.text or "")[:_TEXT_SNIPPET_LIMIT],
                "item_name": row.name,
            }
            for row in samples
        ]
        return {
            "user_id": user_row.id,
            "dataset": user_row.dataset,
            "avg_stars": meta.get("avg_stars"),
            "n_reviews": meta.get("n_reviews"),
            "top_categories": meta.get("top_categories", []),
            "sample_reviews": sample_reviews,
            "user_vector": meta.get("user_vector"),
        }


async def fetch_item(item_id: str) -> dict[str, Any] | None:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT id, dataset, name, metadata FROM items WHERE id = :id"),
                {"id": item_id},
            )
        ).first()
        if not row:
            return None
        return {
            "item_id": row.id,
            "dataset": row.dataset,
            "name": row.name,
            "metadata": row.metadata or {},
        }


async def fetch_user_history(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Most recent training reviews for ``user_id`` (newest first)."""
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT r.item_id, r.stars, r.text, i.name, i.metadata
                FROM reviews r
                JOIN items i ON i.id = r.item_id
                WHERE r.user_id = :uid AND r.is_holdout = FALSE
                ORDER BY r.reviewed_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": limit},
        )
        return [
            {
                "item_id": row.item_id,
                "stars": row.stars,
                "text": (row.text or "")[:_HISTORY_TEXT_LIMIT],
                "name": row.name,
                "metadata": row.metadata or {},
            }
            for row in result
        ]


async def get_user_vector(user_id: str) -> list[float] | None:
    profile = await fetch_user_profile(user_id)
    return profile.get("user_vector") if profile else None


async def list_sample_user_ids(dataset: str = "yelp", limit: int = 5) -> list[str]:
    """Return the top users by review count — ideal for demo requests."""
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT id FROM users
                WHERE dataset = :ds
                ORDER BY (persona_meta->>'n_reviews')::int DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"ds": dataset, "limit": limit},
        )
        return [row.id for row in result]


async def list_sample_items(dataset: str = "yelp", limit: int = 5) -> list[dict[str, Any]]:
    """Return real items (id + name) the API can use in DB-mode demos."""
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT i.id, i.name
                FROM items i
                JOIN (
                    SELECT item_id, COUNT(*) AS n
                    FROM reviews
                    WHERE dataset = :ds
                    GROUP BY item_id
                ) r ON r.item_id = i.id
                WHERE i.dataset = :ds
                ORDER BY r.n DESC
                LIMIT :limit
                """
            ),
            {"ds": dataset, "limit": limit},
        )
        return [{"item_id": row.id, "name": row.name} for row in result]


async def get_dataset_stats() -> dict[str, Any]:
    """Per-dataset and overall counts for the demo / health endpoints."""
    async with get_session() as session:
        per_dataset = await session.execute(
            text(
                """
                SELECT
                    COALESCE(u.dataset, i.dataset, r.dataset) AS dataset,
                    COALESCE(u.n_users, 0)          AS users,
                    COALESCE(i.n_items, 0)          AS items,
                    COALESCE(r.n_reviews, 0)        AS reviews,
                    COALESCE(r.n_holdout, 0)        AS holdout_reviews
                FROM (
                    SELECT dataset, COUNT(*) AS n_users FROM users GROUP BY dataset
                ) u
                FULL OUTER JOIN (
                    SELECT dataset, COUNT(*) AS n_items FROM items GROUP BY dataset
                ) i USING (dataset)
                FULL OUTER JOIN (
                    SELECT dataset,
                           COUNT(*)                            AS n_reviews,
                           COUNT(*) FILTER (WHERE is_holdout)  AS n_holdout
                    FROM reviews GROUP BY dataset
                ) r USING (dataset)
                ORDER BY dataset NULLS LAST
                """
            )
        )
        rows = [dict(row._mapping) for row in per_dataset]

        totals = (
            await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM users)                              AS users,
                        (SELECT COUNT(*) FROM items)                              AS items,
                        (SELECT COUNT(*) FROM reviews)                            AS reviews,
                        (SELECT COUNT(*) FROM reviews WHERE is_holdout)           AS holdout_reviews
                    """
                )
            )
        ).first()
        return {
            "datasets": rows,
            "users": int(totals.users or 0),
            "items": int(totals.items or 0),
            "reviews": int(totals.reviews or 0),
            "holdout_reviews": int(totals.holdout_reviews or 0),
        }


async def database_ping() -> tuple[bool, float | None, str | None]:
    """Return (ok, latency_ms, error) for a trivial round-trip."""
    import time

    start = time.perf_counter()
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # broad: any connection or auth issue counts as down
        return False, None, str(exc)
    return True, round((time.perf_counter() - start) * 1000.0, 2), None
