"""One-time ingestion: extract -> clean (LLM) -> chunk -> embed -> Qdrant.

A PDF is processed ONCE. Its SHA-256 hash is stored in data/registry.json, so asking the
system to ingest the same PDF again is skipped (unless force=True). After that the user can
ask unlimited questions; queries only touch the vector database.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from . import chunk, clean, config, embed, extract, vectorstore
from .domains import Domain, get_domain


# ---- registry of already-ingested files ----------------------------------- #
def _load_registry() -> dict:
    if config.REGISTRY_FILE.exists():
        return json.loads(config.REGISTRY_FILE.read_text(encoding="utf-8"))
    return {}


def _save_registry(registry: dict) -> None:
    config.ensure_dirs()
    config.REGISTRY_FILE.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def list_ingested(domain_key: str) -> dict[str, dict]:
    """{file_hash: {name, pages, chunks, ingested_at}} for one assistant."""
    return _load_registry().get(domain_key, {})


def is_ingested(domain_key: str, pdf_path: Path) -> bool:
    digest = extract.file_sha256(Path(pdf_path))
    return digest in list_ingested(domain_key) and vectorstore.count(get_domain(domain_key).collection) > 0


def status(domain_key: str) -> dict:
    domain = get_domain(domain_key)
    return {"domain": domain_key, "chunks": vectorstore.count(domain.collection), "files": list_ingested(domain_key)}


# ---- main entry point ------------------------------------------------------ #
def ingest_pdf(
    domain: Domain | str,
    pdf_path: Path | str,
    force: bool = False,
    use_llm: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Ingest one PDF into the domain's collection. Returns a small report dict."""
    domain = get_domain(domain) if isinstance(domain, str) else domain
    pdf_path = Path(pdf_path)
    say = progress or (lambda _m: None)
    config.ensure_dirs()

    file_hash = extract.file_sha256(pdf_path)
    registry = _load_registry()
    known = registry.get(domain.key, {})

    if file_hash in known and not force and vectorstore.count(domain.collection) > 0:
        say(f"'{pdf_path.name}' is already in the vector database - skipped (use force to rebuild).")
        return {"status": "skipped", "file": pdf_path.name, **known[file_hash]}

    started = time.time()
    if force and file_hash in known:
        say("force: removing the old vectors of this file")
        vectorstore.delete_file(domain.collection, file_hash)

    say("STEP 1-3  extracting text, detecting images, OCR of text inside images")
    raw_pages = extract.extract_pdf(pdf_path, use_cache=not force, progress=say)

    say("STEP 4    LLM normalisation (merge Data01 + Data02, remove duplicates)")
    clean_pages = clean.clean_pages(raw_pages, use_llm=use_llm, force=force, progress=say)

    say("STEP 5    chunking")
    chunks = chunk.chunk_pages(clean_pages, domain.key, pdf_path.name, file_hash)
    if not chunks:
        raise RuntimeError(f"No text could be extracted from {pdf_path.name}.")
    say(f"          {len(chunks)} chunks")

    say("STEP 6    embedding")
    embedder = embed.get_embedder()
    vectors = embedder.embed_passages([c["text"] for c in chunks])
    chunks, vectors, removed = embed.drop_near_duplicates(chunks, vectors)
    if removed:
        say(f"          removed {removed} near-duplicate chunk(s)")

    say("STEP 7    storing in Qdrant")
    vectorstore.ensure_collection(domain.collection, embedder.dim)
    vectorstore.upsert(domain.collection, chunks, vectors)

    report = {
        "name": pdf_path.name,
        "pages": len(raw_pages),
        "chunks": len(chunks),
        "near_duplicates_removed": removed,
        "image_pages": sum(1 for p in raw_pages if p["is_image"]),
        "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    registry.setdefault(domain.key, {})[file_hash] = report
    _save_registry(registry)
    say(f"done in {time.time() - started:.1f}s")
    return {"status": "ingested", **report}


def ingest_default_pdfs(domain_key: str, **kwargs) -> list[dict]:
    domain = get_domain(domain_key)
    return [ingest_pdf(domain, pdf, **kwargs) for pdf in domain.default_pdfs()]


def reset_domain(domain_key: str) -> None:
    """Delete the collection and forget its files (the PDFs themselves are not touched)."""
    domain = get_domain(domain_key)
    vectorstore.reset_collection(domain.collection)
    registry = _load_registry()
    registry.pop(domain_key, None)
    _save_registry(registry)
