"""Embedding backends for the hackathon stack.

Three backends are supported, selected by ``HACKATHON_EMBEDDING_BACKEND``:

* ``voyage`` (default) — Voyage AI hosted embeddings (1024-d), same model the
  Entivia production stack runs on.
* ``fastembed`` — local ONNX runner shipped by Qdrant; no API calls.
* ``pseudo`` — deterministic hash vectors used by offline smoke tests.

A small dispatch layer (:func:`embed_documents`, :func:`embed_query`) lets the
rest of the codebase stay agnostic of which backend is active.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from functools import lru_cache
from typing import Sequence

import httpx

from hackathon.config import (
    EMBEDDING_BACKEND,
    FASTEMBED_MODEL,
    USE_PSEUDO_EMBEDDINGS,
    VECTOR_SIZE,
    VOYAGE_MODEL,
)

logger = logging.getLogger(__name__)


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


# ── Voyage backend ────────────────────────────────────────────────────────────
# Same provider/model the production Entivia stack uses; we just call the
# REST API synchronously here so the loader can stay sync-friendly.

_VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
_VOYAGE_BATCH = 96
_VOYAGE_MAX_RETRIES = 4


def _voyage_call(texts: list[str], input_type: str) -> list[list[float]]:
    api_key = (os.getenv("VOYAGEAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "VOYAGEAI_API_KEY is required for HACKATHON_EMBEDDING_BACKEND=voyage"
        )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": VOYAGE_MODEL, "input": texts, "input_type": input_type}
    last_err: Exception | None = None
    for attempt in range(_VOYAGE_MAX_RETRIES):
        try:
            resp = httpx.post(_VOYAGE_API_URL, json=payload, headers=headers, timeout=120)
        except httpx.HTTPError as exc:
            last_err = exc
            time.sleep(2**attempt)
            continue
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", "2"))
            logger.warning("Voyage 429, sleeping %.1fs (attempt %d)", wait, attempt + 1)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            last_err = RuntimeError(f"Voyage {resp.status_code}: {resp.text[:300]}")
            if attempt < _VOYAGE_MAX_RETRIES - 1:
                time.sleep(2**attempt)
                continue
            raise last_err
        body = resp.json()
        out: list[list[float] | None] = [None] * len(texts)
        for item in body.get("data", []):
            out[item["index"]] = item["embedding"]
        if any(v is None for v in out):
            raise RuntimeError("Voyage returned partial result")
        return out  # type: ignore[return-value]
    raise RuntimeError(f"Voyage failed after {_VOYAGE_MAX_RETRIES} retries: {last_err}")


def _voyage_documents(texts: Sequence[str], batch_size: int) -> list[list[float]]:
    out: list[list[float]] = []
    bs = min(batch_size, _VOYAGE_BATCH)
    for start in range(0, len(texts), bs):
        out.extend(_voyage_call(list(texts[start : start + bs]), input_type="document"))
    return out


# ── Public dispatch API ───────────────────────────────────────────────────────

def embed_documents(texts: Sequence[str], *, batch_size: int = 128) -> list[list[float]]:
    """Embed a batch of documents using the configured backend."""
    if not texts:
        return []
    if USE_PSEUDO_EMBEDDINGS:
        return [pseudo_embed(t) for t in texts]
    if EMBEDDING_BACKEND == "voyage":
        return _voyage_documents(texts, batch_size)
    if EMBEDDING_BACKEND == "fastembed":
        return _fastembed_documents(texts, batch_size)
    raise RuntimeError(f"Unsupported HACKATHON_EMBEDDING_BACKEND={EMBEDDING_BACKEND!r}")


def embed_query(text: str) -> list[float]:
    """Embed a single query string using the configured backend."""
    if USE_PSEUDO_EMBEDDINGS:
        return pseudo_embed(text)
    if EMBEDDING_BACKEND == "voyage":
        return _voyage_call([text], input_type="query")[0]
    return embed_documents([text], batch_size=1)[0]
