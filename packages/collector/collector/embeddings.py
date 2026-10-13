"""Embeddings for AI asset descriptions, behind a pluggable backend.

Backends (``PS_EMBEDDINGS_BACKEND``):

- ``auto`` (default): ``openai`` when ``OPENAI_API_KEY`` is set, otherwise ``none``.
- ``openai``: POSTs asset metadata to an OpenAI-compatible ``/embeddings`` API.
  ``PS_EMBEDDINGS_OPENAI_BASE_URL`` points it at another provider (e.g. an EU
  endpoint). Data leaves the box.
- ``local``: computes embeddings in-process with fastembed (ONNX, no torch).
  Install with ``pip install 'prompt-shields-collector[local-embeddings]'``.
  Asset metadata never leaves the box; only the model weights are downloaded,
  once, unless ``PS_EMBEDDINGS_CACHE_DIR`` already holds them.
- ``none``: no embeddings; ``/search`` falls back to keyword matching.

Every backend's vectors are zero-padded to ``EMBEDDING_DIM`` so they fit the
``ai_assets.embedding vector(1536)`` column. Appending zeros changes neither
dot products nor norms, so cosine distance between two padded vectors equals
the distance between the originals. Vectors from different models are not
comparable, though: after switching backend or model, re-embed existing assets
with ``python -m collector.reembed``.
"""

import asyncio
import hashlib
import logging
import os
import threading
import time
from typing import Protocol

import httpx

from collector.config import settings

logger = logging.getLogger("prompt_shields.embeddings")

# Width of ai_assets.embedding (packages/db/models.py, alembic 002).
EMBEDDING_DIM = 1536

DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"  # 384 dims, ~65 MB ONNX
BACKENDS = ("auto", "openai", "local", "none")
LOAD_RETRY_SECONDS = 300


class Embedder(Protocol):
    name: str

    async def embed(self, text: str) -> list[float] | None:
        """Return a vector of at most EMBEDDING_DIM floats, or None on failure."""
        ...


def fit_to_column(vector: list[float]) -> list[float]:
    """Zero-pad ``vector`` to EMBEDDING_DIM. Cosine distance is preserved."""
    if len(vector) > EMBEDDING_DIM:
        raise ValueError(
            f"embedding has {len(vector)} dims; the ai_assets.embedding column "
            f"holds {EMBEDDING_DIM}. Choose a model with <= {EMBEDDING_DIM} dims."
        )
    return [float(v) for v in vector] + [0.0] * (EMBEDDING_DIM - len(vector))


class OpenAIEmbedder:
    """OpenAI or any OpenAI-compatible ``/embeddings`` endpoint. Sends data off-box."""

    def __init__(self, api_key: str, model: str, base_url: str):
        self.api_key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/embeddings"
        self.name = f"openai:{model}"

    async def embed(self, text: str) -> list[float] | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.model, "input": text},
                )
            if resp.status_code != 200:
                logger.warning(f"Embedding API returned {resp.status_code}")
                return None
            return resp.json()["data"][0]["embedding"]
        except Exception as e:  # noqa: BLE001 - fail-open by design
            logger.warning(f"Embedding generation failed: {e}")
            return None


class LocalEmbedder:
    """In-process fastembed model. No asset metadata leaves the box."""

    def __init__(self, model: str, cache_dir: str | None = None):
        try:
            import fastembed  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "PS_EMBEDDINGS_BACKEND=local needs fastembed: "
                "pip install 'prompt-shields-collector[local-embeddings]'"
            ) from e
        self.model_name = model
        self.cache_dir = cache_dir
        self.name = f"local:{model}"
        self._model = None
        self._lock = threading.Lock()
        self._load_failed_at: float | None = None

    def _load(self):
        with self._lock:
            if self._model is None:
                # A failed load (e.g. no route to the model hub) retries with
                # long sleeps inside fastembed; don't pay that on every request.
                failed_at = self._load_failed_at
                if (
                    failed_at is not None
                    and time.monotonic() - failed_at < LOAD_RETRY_SECONDS
                ):
                    raise RuntimeError(
                        f"model {self.model_name} unavailable; retrying later"
                    )
                from fastembed import TextEmbedding

                try:
                    self._model = TextEmbedding(
                        model_name=self.model_name, cache_dir=self.cache_dir
                    )
                except Exception:
                    self._load_failed_at = time.monotonic()
                    raise
        return self._model

    def _embed_sync(self, text: str) -> list[float]:
        return next(iter(self._load().embed([text]))).tolist()

    async def embed(self, text: str) -> list[float] | None:
        try:
            return await asyncio.to_thread(self._embed_sync, text)
        except Exception as e:  # noqa: BLE001 - fail-open by design
            logger.warning(f"Local embedding generation failed: {e}")
            return None


def build_embedder() -> Embedder | None:
    """Build the embedder selected by settings; None means semantic search is off."""
    backend = settings.embeddings_backend.lower()
    if backend not in BACKENDS:
        raise ValueError(
            f"PS_EMBEDDINGS_BACKEND={backend!r}; expected one of {', '.join(BACKENDS)}"
        )
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if backend == "auto":
        backend = "openai" if api_key else "none"

    if backend == "none":
        return None
    if backend == "openai":
        if not api_key:
            raise ValueError("PS_EMBEDDINGS_BACKEND=openai requires OPENAI_API_KEY")
        return OpenAIEmbedder(
            api_key=api_key,
            model=settings.embeddings_model or DEFAULT_OPENAI_MODEL,
            base_url=settings.embeddings_openai_base_url,
        )
    return LocalEmbedder(
        model=settings.embeddings_model or DEFAULT_LOCAL_MODEL,
        cache_dir=settings.embeddings_cache_dir,
    )


_UNSET = object()
_embedder = _UNSET


def get_embedder() -> Embedder | None:
    """The process-wide embedder, built on first use."""
    global _embedder
    if _embedder is _UNSET:
        _embedder = build_embedder()
        logger.info(
            "Embeddings backend: %s",
            _embedder.name if _embedder else "none (keyword search)",
        )
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    """Override the process-wide embedder (tests, or an app that wires its own)."""
    global _embedder
    _embedder = embedder


def reset_embedder() -> None:
    """Forget the cached embedder so the next call rebuilds it from settings."""
    global _embedder
    _embedder = _UNSET


def build_asset_text(
    vendor: str,
    model: str | None,
    use_case_name: str | None,
    business_unit: str | None,
    data_classification: str | None,
) -> str:
    """Build a descriptive text string from asset metadata for embedding."""
    parts = [f"AI vendor: {vendor}"]
    if model:
        parts.append(f"model: {model}")
    if use_case_name:
        parts.append(f"use case: {use_case_name}")
    if business_unit:
        parts.append(f"business unit: {business_unit}")
    if data_classification:
        parts.append(f"data classification: {data_classification}")
    return ", ".join(parts)


async def get_embedding(text: str) -> list[float] | None:
    """Embed ``text`` with the configured backend, padded to EMBEDDING_DIM.

    Returns None when semantic search is disabled or the backend fails; callers
    treat that as "no embedding" (ingest) or fall back to keyword search.
    """
    embedder = get_embedder()
    if embedder is None:
        return None
    vector = await embedder.embed(text)
    if vector is None:
        return None
    try:
        return fit_to_column(vector)
    except ValueError as e:
        logger.warning(str(e))
        return None


def mock_embedding(text: str) -> list[float]:
    """Generate a deterministic mock embedding for testing (no API key needed)."""
    h = hashlib.sha256(text.encode()).hexdigest()
    # Generate 1536 floats from hash bytes, cycling through
    values = []
    for i in range(EMBEDDING_DIM):
        byte_idx = i % 32
        values.append((int(h[byte_idx * 2 : byte_idx * 2 + 2], 16) - 128) / 128.0)
    return values
