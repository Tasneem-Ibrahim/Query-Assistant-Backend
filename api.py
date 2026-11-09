"""REST API for the frontend.   Run:  uvicorn api:app --port 8000"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from rag import answer, ingest, extract, config  # noqa: E402
from rag.domains import DOMAINS  # noqa: E402

app = FastAPI(title="Query Assistant API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _domain(key: str):
    if key not in DOMAINS:
        raise HTTPException(404, f"Unknown assistant '{key}'. Use: {', '.join(DOMAINS)}")
    return DOMAINS[key]


class Ask(BaseModel):
    domain: str
    question: str
    history: list[dict] = []


@app.get("/api/domains")
def domains():
    return [{"key": d.key, "label": d.label, "icon": d.icon, "examples": d.examples} for d in DOMAINS.values()]


@app.get("/api/status/{key}")
def status(key: str):
    _domain(key)
    return ingest.status(key)


@app.post("/api/ingest/{key}")
def ingest_pdf(key: str, file: UploadFile | None = File(None)):
    domain = _domain(key)
    if file is not None:
        domain.pdf_dir.mkdir(parents=True, exist_ok=True)
        target = domain.pdf_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename or "upload.pdf")
        content = file.file.read(config.MAX_UPLOAD_BYTES + 1)
        if len(content) > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Choose a document smaller than 4 MB.")
        try:
            extract.document_pdf(target.name, content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        target.write_bytes(content)
        pdfs = [target]
    else:
        pdfs = domain.default_pdfs()
    if not pdfs:
        raise HTTPException(400, "No PDF found for this assistant.")
    try:
        return {"reports": [ingest.ingest_pdf(domain, p) for p in pdfs]}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/ask")
def ask(body: Ask):
    domain = _domain(body.domain)
    try:
        r = answer.ask(domain, body.question, history=body.history)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"answer": r["answer"], "sources": [{**h["metadata"], "text": h["text"]} for h in r["sources"]]}
