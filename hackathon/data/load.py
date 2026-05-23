"""Hackathon data loader CLI.

Resets the Postgres schema and (optionally) the Qdrant collection, ingests
Yelp (real or synthetic) plus a Goodreads slice, embeds every item with the
configured backend, and writes the holdout split to
``eval/data/holdout_yelp.jsonl``.

Usage::

    # Full local hackathon stack (Postgres + Qdrant + embeddings + Goodreads):
    python -m hackathon.data.load

    # Load the Yelp slice into an EXTERNAL Postgres (e.g. the database that
    # backs the "Yelp Demo" connection on entivia.online). Skip Qdrant and
    # Goodreads — the platform's pipeline will profile and embed on its own.
    HACKATHON_DATABASE_URL='postgresql+asyncpg://USER:PASS@HOST:PORT/DB?ssl=require' \
    HACKATHON_YELP_DIR=~/datasets/yelp \
    python -m hackathon.data.load --no-vector --no-goodreads
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from hackathon.config import ALLOW_SYNTHETIC, HACKATHON_YELP_DIR
from hackathon.core import db as hack_db
from hackathon.data.goodreads import load_goodreads
from hackathon.data.synthetic import seed_synthetic_yelp
from hackathon.data.yelp import load_yelp

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("hackathon.loader")

HOLDOUT_PATH = Path(__file__).resolve().parent.parent / "eval" / "data" / "holdout_yelp.jsonl"


async def _load_yelp_source(session) -> dict:
    if HACKATHON_YELP_DIR and HACKATHON_YELP_DIR.is_dir():
        return await load_yelp(session)
    if ALLOW_SYNTHETIC:
        logger.info("Yelp files not found at %s — seeding synthetic data", HACKATHON_YELP_DIR)
        return await seed_synthetic_yelp(session)
    raise SystemExit(
        "Set HACKATHON_YELP_DIR to a Yelp JSON folder or HACKATHON_ALLOW_SYNTHETIC=1"
    )


def _write_holdout(rows: list[dict]) -> None:
    HOLDOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HOLDOUT_PATH, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    logger.info("Wrote %d holdout rows to %s", len(rows), HOLDOUT_PATH)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Yelp/Goodreads into Postgres.")
    parser.add_argument(
        "--no-vector",
        action="store_true",
        help="Skip Qdrant reset and item embedding. Use when loading into an external "
        "Postgres that will be wired up via Entivia's Connections UI (the platform "
        "will embed via its own Qdrant + Voyage/fastembed during the pipeline run).",
    )
    parser.add_argument(
        "--no-goodreads",
        action="store_true",
        help="Skip the Goodreads cross-domain slice (it's only needed for the hackathon "
        "Task B cross-domain demo, not for a tenant-style external DB).",
    )
    parser.add_argument(
        "--no-holdout",
        action="store_true",
        help="Skip writing the holdout JSONL (only relevant for hackathon eval).",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    await hack_db.apply_schema()
    await hack_db.truncate_all()

    if not args.no_vector:
        from hackathon.core.vector_store import vector_store

        await vector_store.reset_collection()

    async with hack_db.get_session() as session:
        stats = await _load_yelp_source(session)
        logger.info(
            "Yelp loaded: users=%s items=%s reviews=%s holdout=%s",
            stats["users"],
            stats["items"],
            stats["reviews"],
            len(stats["holdout"]),
        )

        if not args.no_goodreads:
            n_goodreads = await load_goodreads(session)
            logger.info("Goodreads loaded: %d items", n_goodreads)

        if not args.no_vector:
            from hackathon.data.embed import embed_all_items

            await embed_all_items(session)

        await session.commit()

    if not args.no_holdout:
        _write_holdout(stats["holdout"])


if __name__ == "__main__":
    asyncio.run(main())

# cd /Users/ozigbochidera/Desktop/api



