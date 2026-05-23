"""Async Postgres access for the hackathon tables (`users`, `items`, `reviews`)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hackathon.config import HACKATHON_DATABASE_URL

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "schema.sql"

_engine = create_async_engine(
    HACKATHON_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def apply_schema() -> None:
    """Apply schema.sql idempotently (uses IF NOT EXISTS clauses)."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with _engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in sql.split(";"))):
            await conn.execute(text(statement))


async def truncate_all() -> None:
    async with _engine.begin() as conn:
        await conn.execute(text("TRUNCATE reviews, items, users CASCADE"))


def _jsonb(value: Any) -> str:
    return json.dumps(value, default=str)


async def upsert_user(
    session: AsyncSession,
    user_id: str,
    dataset: str,
    persona_meta: dict[str, Any],
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO users (id, dataset, persona_meta)
            VALUES (:id, :dataset, CAST(:meta AS jsonb))
            ON CONFLICT (id) DO UPDATE SET persona_meta = EXCLUDED.persona_meta
            """
        ),
        {"id": user_id, "dataset": dataset, "meta": _jsonb(persona_meta)},
    )


async def upsert_item(
    session: AsyncSession,
    item_id: str,
    dataset: str,
    name: str,
    metadata: dict[str, Any],
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO items (id, dataset, name, metadata)
            VALUES (:id, :dataset, :name, CAST(:meta AS jsonb))
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                metadata = EXCLUDED.metadata
            """
        ),
        {"id": item_id, "dataset": dataset, "name": name, "meta": _jsonb(metadata)},
    )


async def insert_review(
    session: AsyncSession,
    review_id: str,
    user_id: str,
    item_id: str,
    dataset: str,
    stars: float,
    text_body: str,
    reviewed_at: datetime | None,
    is_holdout: bool,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO reviews (id, user_id, item_id, dataset, stars, text, reviewed_at, is_holdout)
            VALUES (:id, :uid, :iid, :dataset, :stars, :txt, :ts, :holdout)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": review_id,
            "uid": user_id,
            "iid": item_id,
            "dataset": dataset,
            "stars": stars,
            "txt": text_body,
            "ts": reviewed_at,
            "holdout": is_holdout,
        },
    )
