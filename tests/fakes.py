"""Offline stand-ins so the pipeline can be tested without downloading a model or calling an API."""

from __future__ import annotations

import hashlib
import re

import numpy as np


class FakeEmbedder:
    """Bag-of-words hashing embedder: not semantic, but similar words -> similar vectors."""

    dim = 256
    model_name = "fake-hash"

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype="float32")
        for tok in re.findall(r"[a-z0-9$%.]+", text.lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_passages(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)
