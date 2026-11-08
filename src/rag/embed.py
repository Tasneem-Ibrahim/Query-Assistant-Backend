"""STEP 6: embeddings (sentence-transformers). The same model is used for indexing and querying."""

from __future__ import annotations

import numpy as np

from . import config

_embedder = None


class APIEmbedder:
    """OpenAI-compatible /embeddings endpoint; no local model or torch import."""

    def __init__(self):
        if not all([config.EMBEDDING_API_URL, config.EMBEDDING_API_KEY, config.EMBEDDING_API_MODEL, config.EMBEDDING_DIMENSIONS]):
            raise ValueError("Configure EMBEDDING_API_URL, EMBEDDING_API_KEY, EMBEDDING_API_MODEL and EMBEDDING_DIMENSIONS.")
        self.model_name = config.EMBEDDING_API_MODEL
        self.dim = config.EMBEDDING_DIMENSIONS

    def _encode(self, texts):
        import httpx

        batches = []
        for start in range(0, len(texts), 16):
            response = httpx.post(config.EMBEDDING_API_URL,
                headers={"Authorization": f"Bearer {config.EMBEDDING_API_KEY}"},
                json={"model": self.model_name, "input": texts[start:start + 16]}, timeout=60)
            if not response.is_success:
                raise RuntimeError(f"Embedding provider returned HTTP {response.status_code}. Check its configuration and quota.")
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            if [item["index"] for item in data] != list(range(len(texts[start:start + 16]))):
                raise ValueError("Embedding provider returned missing or duplicate vectors.")
            vectors = np.asarray([item["embedding"] for item in data], dtype="float32")
            if vectors.shape != (len(data), self.dim) or not np.isfinite(vectors).all():
                raise ValueError("Embedding dimensions do not match EMBEDDING_DIMENSIONS.")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if (norms == 0).any():
                raise ValueError("Embedding provider returned an empty vector.")
            batches.append(vectors / norms)
        return np.concatenate(batches) if batches else np.empty((0, self.dim), dtype="float32")

    def embed_passages(self, texts):
        return self._encode(texts)

    def embed_query(self, text):
        return self._encode([text])[0]


class Embedder:
    """multilingual-e5 models need the prefixes 'passage: ' and 'query: ' for best accuracy."""

    def __init__(self, model_name: str | None = None):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name or config.EMBEDDING_MODEL
        self.model = SentenceTransformer(self.model_name)
        self.use_prefix = "e5" in self.model_name.lower()
        self.dim = int(self.model.get_sentence_embedding_dimension())

    def _encode(self, texts: list[str]) -> np.ndarray:
        vectors = self.model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vectors, dtype="float32")

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._encode([f"passage: {t}" for t in texts] if self.use_prefix else texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([f"query: {text}" if self.use_prefix else text])[0]


def get_embedder():
    """Load the model once per process (loading takes a few seconds)."""
    global _embedder
    if _embedder is None:
        _embedder = APIEmbedder() if config.EMBEDDING_PROVIDER == "api" else Embedder()
    return _embedder


def set_embedder(embedder) -> None:
    """Swap the embedder (used by the offline tests)."""
    global _embedder
    _embedder = embedder


def drop_near_duplicates(chunks: list[dict], vectors: np.ndarray, threshold: float | None = None):
    """Remove chunks whose embedding is almost identical (cosine >= threshold) to an earlier chunk."""
    threshold = config.NEAR_DUPLICATE_THRESHOLD if threshold is None else threshold
    if len(chunks) < 2:
        return chunks, vectors, 0
    sims = vectors @ vectors.T  # vectors are normalised -> cosine similarity
    keep: list[int] = []
    for i in range(len(chunks)):
        if all(sims[i, j] < threshold for j in keep):
            keep.append(i)
    removed = len(chunks) - len(keep)
    return [chunks[i] for i in keep], vectors[keep], removed
