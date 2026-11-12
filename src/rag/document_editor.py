"""Edit the actual indexed chunks, preserving the original upload."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from qdrant_client import models
from . import chunk, cloud, config, embed, ingest, vectorstore

_lock = threading.RLock()


class EditConflict(ValueError):
    pass


def _document(domain, digest):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid document identifier.")
    if config.CLOUD_MODE:
        row = next((r for r in cloud.documents(domain.key) if r["file_hash"] == digest), None)
        if row and row["status"] != "ready":
            raise EditConflict("Finish processing this document before editing it.")
    else:
        row = ingest.list_ingested(domain.key).get(digest)
    if row is None:
        raise KeyError(digest)
    return row


def _points(domain, digest):
    client = vectorstore.get_client()
    vectorstore._check_workspace(domain.collection)
    if not client.collection_exists(domain.collection):
        return []
    result, offset = [], None
    while True:
        points, offset = client.scroll(
            collection_name=domain.collection,
            scroll_filter=models.Filter(must=[models.FieldCondition(
                key="file_hash", match=models.MatchValue(value=digest))]),
            limit=256, offset=offset, with_payload=True, with_vectors=True)
        result.extend(points)
        if offset is None:
            break
    return sorted(result, key=lambda p: (p.payload.get("page", 1),
                                        p.payload.get("chunk_index", 0), str(p.id)))


def _revision(points):
    data = [(str(p.id), p.payload) for p in points]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _view(row, points):
    return {"name": row["name"], "pages": row["pages"], "revision": _revision(points),
            "chunks": [{"text": p.payload["text"], "page": p.payload.get("page", 1)}
                       for p in points]}


def read(domain, digest):
    with _lock:
        row = _document(domain, digest)
        if config.CLOUD_MODE and row.get("lease_until"):
            expires = datetime.fromisoformat(row["lease_until"].replace("Z", "+00:00"))
            if expires > datetime.now(timezone.utc):
                raise EditConflict("Document is being processed or edited. Retry shortly.")
        return _view(row, _points(domain, digest))


@contextmanager
def _lease(domain, digest):
    if not config.CLOUD_MODE:
        yield None
        return
    args = {"p_namespace": config.embedding_namespace(), "p_domain": domain.key,
            "p_hash": digest, "p_token": str(uuid.uuid4())}
    claimed = cloud._request("POST", "/rest/v1/rpc/qa_claim_document",
                             json={**args, "p_delete": True}).json()
    if not claimed:
        raise EditConflict("Document is being processed or edited. Retry shortly.")
    try:
        yield args
    finally:
        cloud._release(args)


def save(domain, digest, revision, entries):
    # Serialize local edits; cloud additionally uses the existing database lease.
    with _lock:
        _document(domain, digest)
        return _save(domain, digest, revision, entries)


def _save(domain, digest, revision, entries):
    with _lease(domain, digest) as lease:
        row = _document(domain, digest)
        old = _points(domain, digest)
        if revision != _revision(old):
            raise EditConflict("This document changed. Reopen it before saving your edits.")
        if not entries or len(entries) > 1000:
            raise ValueError("Provide between 1 and 1000 chunks.")
        if sum(len(e["text"]) for e in entries) > 1_000_000:
            raise ValueError("Edited text must be at most 1,000,000 characters.")
        cleaned, seen = [], set()
        for entry in entries:
            page = entry["page"]
            if page < 1 or page > row["pages"]:
                raise ValueError("Choose an original page number for each chunk.")
            text = unicodedata.normalize("NFC", entry["text"]).replace("\r\n", "\n")
            text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()).strip()
            # Exact normalized chunk duplicates are removed without rewriting facts.
            identity = " ".join(text.split()).casefold()
            if not text or identity in seen:
                continue
            seen.add(identity)
            cleaned.extend(chunk.chunk_pages(
                [{"page": page, "sections": [{"heading": "", "content": text}]}],
                domain.key, row["name"], digest))
        if not cleaned:
            raise ValueError("Keep at least one non-empty chunk.")
        # Fresh IDs avoid collisions when several editor chunks share a page.
        per_page = {}
        for part in cleaned:
            page = part["metadata"]["page"]
            index = per_page.get(page, 0)
            per_page[page] = index + 1
            part["metadata"]["chunk_index"] = index
            part["id"] = str(uuid.uuid4())
        embedder = embed.get_embedder()
        vectors = embedder.embed_passages([p["text"] for p in cleaned])
        cleaned, vectors, removed = embed.drop_near_duplicates(cleaned, vectors)
        # All expensive validation/embedding finishes before touching saved vectors.
        if config.CLOUD_MODE:
            valid = cloud.rows("qa_documents", **cloud.scope(domain.key),
                file_hash="eq." + digest, lease_token="eq." + lease["p_token"],
                lease_until="gt." + datetime.now(timezone.utc).isoformat())
            if not valid:
                raise EditConflict("Editing lease expired. Retry saving.")
        client = vectorstore.get_client()
        vectorstore.ensure_collection(domain.collection, embedder.dim)
        new_ids = [p["id"] for p in cleaned]
        try:
            vectorstore.upsert(domain.collection, cleaned, vectors)
            if old:
                client.delete(collection_name=domain.collection,
                              points_selector=models.PointIdsList(points=[p.id for p in old]), wait=True)
            if config.CLOUD_MODE:
                updated = cloud._request("PATCH", "/rest/v1/qa_documents",
                    params={**cloud.scope(domain.key), "file_hash": "eq." + digest,
                            "lease_token": "eq." + lease["p_token"], "lease_until": "gt." + datetime.now(timezone.utc).isoformat()},
                    headers={"Prefer": "return=representation"},
                    json={"chunks": len(cleaned)}).json()
                if not updated:
                    raise EditConflict("Editing lease expired. Retry saving.")
            else:
                registry = ingest._load_registry()
                registry[domain.key][digest]["chunks"] = len(cleaned)
                registry[domain.key][digest]["near_duplicates_removed"] = removed
                ingest._save_registry(registry)
        except Exception:
            # Recover the previous content if a vector/metadata write fails.
            if old:
                client.upsert(collection_name=domain.collection,
                    points=[models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in old],
                    wait=True)
            client.delete(collection_name=domain.collection,
                          points_selector=models.PointIdsList(points=new_ids), wait=True)
            raise
        return _view({**row, "chunks": len(cleaned)}, _points(domain, digest))
