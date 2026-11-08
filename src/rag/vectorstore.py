"""STEP 7: Qdrant vector database (one collection per assistant).

Default: embedded local mode (data/qdrant_db, no server needed). Only ONE process can open
a local Qdrant folder at a time, so close the Streamlit app before using the CLI, or run a
Qdrant server (docker run -p 6333:6333 qdrant/qdrant) and set QDRANT_URL in .env.
"""

from __future__ import annotations

import atexit
import threading

import numpy as np
from qdrant_client import QdrantClient, models

from . import config

_client: QdrantClient | None = None
_lock = threading.Lock()


def get_client() -> QdrantClient:
    global _client
    with _lock:
        if _client is None:
            if config.QDRANT_URL:
                _client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY)
            else:
                config.ensure_dirs()
                _client = QdrantClient(path=str(config.QDRANT_PATH))
            atexit.register(close_client)
        return _client


def close_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


def collection_exists(name: str) -> bool:
    return get_client().collection_exists(name)


def _ensure_file_hash_index(client: QdrantClient, name: str) -> None:
    # Cloud strict mode requires an index for filtered deletion. Repair older
    # collections too; embedded Qdrant does not implement payload indexes.
    if config.QDRANT_URL and "file_hash" not in client.get_collection(name).payload_schema:
        client.create_payload_index(
            collection_name=name,
            field_name="file_hash",
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )


def ensure_collection(name: str, dim: int) -> None:
    client = get_client()
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
    _ensure_file_hash_index(client, name)


def reset_collection(name: str) -> None:
    client = get_client()
    if client.collection_exists(name):
        client.delete_collection(name)


def count(name: str) -> int:
    client = get_client()
    return client.count(collection_name=name, exact=True).count if client.collection_exists(name) else 0


def upsert(name: str, chunks: list[dict], vectors: np.ndarray, batch_size: int = 64) -> None:
    client = get_client()
    points = [
        models.PointStruct(id=c["id"], vector=vectors[i].tolist(), payload={"text": c["text"], **c["metadata"]})
        for i, c in enumerate(chunks)
    ]
    for start in range(0, len(points), batch_size):
        client.upsert(collection_name=name, points=points[start : start + batch_size], wait=True)


def delete_file(name: str, file_hash: str) -> None:
    client = get_client()
    if client.collection_exists(name):
        _ensure_file_hash_index(client, name)
        client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(
                filter=models.Filter(must=[models.FieldCondition(key="file_hash", match=models.MatchValue(value=file_hash))])
            ),
            wait=True,
        )


def search(name: str, vector: np.ndarray, top_k: int) -> list[dict]:
    """Similarity search. Returns [{'text', 'metadata', 'score'}], best first."""
    client = get_client()
    if not client.collection_exists(name):
        return []
    query_filter = None
    if config.CLOUD_MODE:
        from . import cloud
        prefix = config.embedding_namespace() + "_"
        if not name.startswith(prefix):
            raise ValueError("Cannot query a collection from another embedding configuration.")
        hashes = list(cloud.list_ingested(name[len(prefix):]))
        if not hashes:
            return []
        query_filter = models.Filter(must=[models.FieldCondition(key="file_hash", match=models.MatchAny(any=hashes))])
    response = client.query_points(collection_name=name, query=vector.tolist(), limit=top_k, with_payload=True, query_filter=query_filter)
    hits = []
    for point in response.points:
        payload = dict(point.payload or {})
        text = payload.pop("text", "")
        hits.append({"text": text, "metadata": payload, "score": float(point.score)})
    return hits
