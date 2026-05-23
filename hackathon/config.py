"""Hackathon runtime configuration.

All values are environment-driven so the same image can run locally, in CI, and
on a VPS without code changes. Defaults assume the bundled docker-compose
stack and the open-source BGE embedding backend.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0") == "1"


# ── Storage ───────────────────────────────────────────────────────────────────

HACKATHON_DATABASE_URL: str = os.getenv(
    "HACKATHON_DATABASE_URL",
    "postgresql+asyncpg://hackathon:hackathon@localhost:5433/hackathon",
)
HACKATHON_QDRANT_COLLECTION: str = os.getenv(
    "HACKATHON_QDRANT_COLLECTION", "hackathon_items"
)


# ── Dataset locations ─────────────────────────────────────────────────────────

HACKATHON_YELP_DIR: Path | None = _env_path("HACKATHON_YELP_DIR")
HACKATHON_GOODREADS_PATH: Path | None = _env_path("HACKATHON_GOODREADS_PATH")


# ── Sampling caps (tune down for faster local dev) ────────────────────────────

MAX_YELP_USERS: int = _env_int("HACKATHON_MAX_YELP_USERS", 5000)
MAX_YELP_ITEMS: int = _env_int("HACKATHON_MAX_YELP_ITEMS", 12_000)
MIN_REVIEWS_PER_USER: int = _env_int("HACKATHON_MIN_REVIEWS", 10)
MAX_GOODREADS_ITEMS: int = _env_int("HACKATHON_MAX_GOODREADS", 1000)
HOLDOUT_FRACTION: float = float(os.getenv("HACKATHON_HOLDOUT_FRACTION", "0.1"))


# ── Embeddings ────────────────────────────────────────────────────────────────
# Voyage is the default for parity with the Entivia production stack. The
# fastembed backend is kept for fully offline runs; pseudo is for smoke tests.

EMBEDDING_BACKEND: str = os.getenv("HACKATHON_EMBEDDING_BACKEND", "voyage").lower()
VOYAGE_MODEL: str = os.getenv("HACKATHON_VOYAGE_MODEL", "voyage-4-large")
FASTEMBED_MODEL: str = os.getenv("HACKATHON_FASTEMBED_MODEL", "BAAI/bge-small-en-v1.5")
USE_PSEUDO_EMBEDDINGS: bool = _env_bool("HACKATHON_USE_PSEUDO_EMBEDDINGS", False)

_VOYAGE_DIMS: dict[str, int] = {
    "voyage-4-large": 1024,
    "voyage-4": 1024,
    "voyage-3.5": 1024,
    "voyage-3.5-lite": 1024,
}
_FASTEMBED_DIMS: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
}
if EMBEDDING_BACKEND == "voyage":
    _DEFAULT_DIM = _VOYAGE_DIMS.get(VOYAGE_MODEL, 1024)
else:
    _DEFAULT_DIM = _FASTEMBED_DIMS.get(FASTEMBED_MODEL, 384)
VECTOR_SIZE: int = _env_int("HACKATHON_VECTOR_SIZE", _DEFAULT_DIM)


# ── Loader behaviour ──────────────────────────────────────────────────────────

ALLOW_SYNTHETIC: bool = _env_bool("HACKATHON_ALLOW_SYNTHETIC", True)
