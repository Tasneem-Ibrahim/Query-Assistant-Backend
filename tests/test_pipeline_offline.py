"""Offline smoke test (no model download, no API key):

    python tests/test_pipeline_offline.py

Runs extract -> clean (rule-based) -> chunk -> embed (fake) -> Qdrant -> search for both assistants
in a temporary folder and checks that ingestion happens only once.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fakes import FakeEmbedder  # noqa: E402

from rag import config, embed, ingest, vectorstore  # noqa: E402
from rag.domains import DOMAINS  # noqa: E402
from rag import answer  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    config.DATA_DIR = tmp
    config.CACHE_DIR = tmp / "cache"
    config.REGISTRY_FILE = tmp / "registry.json"
    config.QDRANT_PATH = tmp / "qdrant_db"
    embed.set_embedder(FakeEmbedder())

    checks = {
        "customer_support": ("Cash on Delivery refund bank transfer or store credit", "7-10 business days (bank transfer or store credit)"),
        "legal_assistant": ("early termination fee three months", "37,500"),
    }
    for key, (query, expected) in checks.items():
        first = ingest.ingest_default_pdfs(key, use_llm=False)
        assert first and all(r["status"] == "ingested" for r in first), first
        second = ingest.ingest_default_pdfs(key, use_llm=False)
        assert all(r["status"] == "skipped" for r in second), "PDF must not be vectorised twice"
        hits = answer.retrieve(key, query, top_k=3)
        assert any(expected in h["text"] for h in hits), f"{key}: '{expected}' not found in top hits"
        print(f"OK {key}: {first[0]['chunks']} chunks, retrieval found '{expected}', second ingest skipped")

    vectorstore.close_client()
    print("ALL OFFLINE CHECKS PASSED")


if __name__ == "__main__":
    main()
