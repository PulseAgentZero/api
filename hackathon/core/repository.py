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
