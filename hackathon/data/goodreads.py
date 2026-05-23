"""Load a Goodreads books slice for the cross-domain demo.

The real Goodreads JSONL dump is large; only ``MAX_GOODREADS_ITEMS`` rows are
ingested. If no file is provided we fall back to the synthetic seed so the
``/recommend`` endpoint always has a `goodreads` dataset to demo.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from hackathon.config import HACKATHON_GOODREADS_PATH, MAX_GOODREADS_ITEMS
from hackathon.core import db as hack_db
from hackathon.data.synthetic import seed_synthetic_goodreads

logger = logging.getLogger(__name__)


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if str(path).endswith(".gz") \
        else open(path, "rt", encoding="utf-8", errors="replace")


async def load_goodreads(session: AsyncSession, path: Path | None = None) -> int:
    source = path or HACKATHON_GOODREADS_PATH
    if not source or not source.is_file():
        logger.warning("No Goodreads file at %s; seeding synthetic books", source)
        return await seed_synthetic_goodreads(session, MAX_GOODREADS_ITEMS)

    count = 0
    with _open(source) as fh:
        for line in fh:
            if count >= MAX_GOODREADS_ITEMS:
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            book_id = str(row.get("book_id") or row.get("work_id") or count)
            title = row.get("title") or row.get("title_without_series") or f"Book {book_id}"
            await hack_db.upsert_item(
                session,
                f"gr_{book_id}",
                "goodreads",
                str(title)[:500],
                {
                    "authors": row.get("authors") or row.get("author_names"),
                    "genres": row.get("genres") or row.get("popular_shelves"),
                },
            )
            count += 1
    logger.info("Loaded %d Goodreads items from %s", count, source)
    return count
