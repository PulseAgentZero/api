"""Embedding backends for the hackathon stack.

Three backends are supported, selected by ``HACKATHON_EMBEDDING_BACKEND``:

* ``fastembed`` (default) — local ONNX runner shipped by Qdrant; no API calls.
* ``pseudo`` — deterministic hash vectors used by offline smoke tests.

A small dispatch layer (:func:`embed_documents`, :func:`embed_query`) lets the
rest of the codebase stay agnostic of which backend is active.
"""

from __future__ import annotations

import hashlib
import math
import os
from functools import lru_cache
from typing import Sequence

from hackathon.config import (
    EMBEDDING_BACKEND,
    FASTEMBED_MODEL,
    USE_PSEUDO_EMBEDDINGS,
    VECTOR_SIZE,
)


# ── Pseudo backend ────────────────────────────────────────────────────────────

def pseudo_embed(text: str, dim: int = VECTOR_SIZE) -> list[float]:
    """Deterministic unit vector — used only when no real backend is configured.

    Useful for tests and offline smoke checks; not meaningful for retrieval.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vec = [(digest[i % len(digest)] / 127.5) - 1.0 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── fastembed backend ─────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _fastembed_model():
    from fastembed import TextEmbedding

    cache_dir = os.getenv("FASTEMBED_CACHE_PATH") or os.getenv("HF_HOME")
    return TextEmbedding(model_name=FASTEMBED_MODEL, cache_dir=cache_dir)


def _as_floats(vector) -> list[float]:
    return vector.tolist() if hasattr(vector, "tolist") else list(vector)


def _fastembed_documents(texts: Sequence[str], batch_size: int) -> list[list[float]]:
    model = _fastembed_model()
    return [_as_floats(v) for v in model.embed(list(texts), batch_size=batch_size)]


# ── Public dispatch API ───────────────────────────────────────────────────────

def embed_documents(texts: Sequence[str], *, batch_size: int = 128) -> list[list[float]]:
    """Embed a batch of documents using the configured backend."""
    if not texts:
        return []
    if USE_PSEUDO_EMBEDDINGS:
        return [pseudo_embed(t) for t in texts]
    if EMBEDDING_BACKEND == "fastembed":
        return _fastembed_documents(texts, batch_size)
    raise RuntimeError(f"Unsupported HACKATHON_EMBEDDING_BACKEND={EMBEDDING_BACKEND!r}")


def embed_query(text: str) -> list[float]:
    """Embed a single query string using the configured backend."""
    return embed_documents([text], batch_size=1)[0]
