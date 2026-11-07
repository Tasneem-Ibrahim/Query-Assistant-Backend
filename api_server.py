"""HTTP bridge between the Query Assistant frontend and your existing RAG backend.

Put this file in your project folder, next to app.py and cli.py. It does not change any of your
code: it only calls the same functions that app.py and cli.py already use
(ingest.ingest_pdf, ingest.status, answer.ask, answer.retrieve, ingest.reset_domain).

    pip install fastapi uvicorn python-multipart
    uvicorn api_server:app --port 8000

Then start the frontend (npm run dev). It finds the API at http://localhost:8000 by itself.

Note: local Qdrant opens its folder in ONE process only. Close Streamlit (and the CLI) while this
server runs, or use a Qdrant server and set QDRANT_URL in .env.
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from rag import answer, config, ingest  # noqa: E402
from rag.domains import create_domain, delete_domain, get_domain, list_domains, rename_domain  # noqa: E402

app = FastAPI(title="Query Assistant API", version="1.0")

# The frontend runs on another port (3000), so allow it. Add your own origin if you deploy it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _domain(key: str):
    try:
        return get_domain(key)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def domain_info(domain):
    return {"key": domain.key, "label": domain.label, "welcome": domain.welcome,
            "examples": domain.examples, "custom": domain.key.startswith("custom_")}


class DomainBody(BaseModel):
    name: str


class AskBody(BaseModel):
    question: str
    history: list[dict] = []
    top_k: int | None = None


class SearchBody(BaseModel):
    query: str
    top_k: int = 5


# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"ok": True, "deepseek_key": bool(config.DEEPSEEK_API_KEY), "qdrant": "server" if config.QDRANT_URL else "local"}


@app.get("/api/domains")
def domains():
    return [domain_info(d) for d in list_domains()]


@app.post("/api/domains", status_code=201)
def new_domain(body: DomainBody):
    try:
        return domain_info(create_domain(body.name))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/domains/{key}")
def update_domain(key: str, body: DomainBody):
    _domain(key)
    try:
        return domain_info(rename_domain(key, body.name))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/domains/{key}")
def remove_domain(key: str):
    _domain(key)
    try:
        delete_domain(key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/api/domains/{key}/status")
def status(key: str):
    _domain(key)
    return ingest.status(key)


@app.post("/api/domains/{key}/ask")
def ask(key: str, body: AskBody):
    domain = _domain(key)
    if not body.question.strip():
        raise HTTPException(400, "Question is empty.")
    try:
        return answer.ask(domain, body.question.strip(), history=body.history, top_k=body.top_k)
    except Exception as exc:  # e.g. missing DEEPSEEK_API_KEY
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/domains/{key}/search")
def search(key: str, body: SearchBody):
    domain = _domain(key)
    try:
        return {"hits": answer.retrieve(domain, body.query.strip(), body.top_k)}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/domains/{key}/reset")
def reset(key: str):
    _domain(key)
    ingest.reset_domain(key)
    return {"ok": True}


@app.post("/api/domains/{key}/ingest")
async def ingest_endpoint(
    key: str,
    file: UploadFile | None = File(default=None),
    force: bool = Form(default=False),
    use_llm: bool = Form(default=True),
):
    """Process one uploaded PDF (or the bundled PDFs when no file is sent).

    The answer is a stream with one JSON object per line:
      {"type": "log", "message": "..."}  while processing
      {"type": "done", "report": {...}}  at the end (report of the last file)
      {"type": "error", "message": "..."}
    """
    domain = _domain(key)

    targets: list[Path]
    if file is not None and file.filename:
        domain.pdf_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename)
        if not safe.lower().endswith(".pdf"):
            raise HTTPException(400, "Only PDF files are supported.")
        target = domain.pdf_dir / safe
        target.write_bytes(await file.read())
        targets = [target]
    else:
        targets = domain.default_pdfs()
        if not targets:
            raise HTTPException(404, f"No bundled PDF found in pdfs/{domain.pdf_subdir}/. Upload one instead.")

    events: "queue.Queue[dict | None]" = queue.Queue()

    def work() -> None:
        try:
            report: dict = {}
            for pdf in targets:
                report = ingest.ingest_pdf(
                    domain, pdf, force=force, use_llm=use_llm, progress=lambda m: events.put({"type": "log", "message": m})
                )
            events.put({"type": "done", "report": report})
        except Exception as exc:
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = events.get()
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
