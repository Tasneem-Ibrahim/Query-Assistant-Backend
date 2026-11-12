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
from contextvars import copy_context
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from rag import answer, config, ingest, extract  # noqa: E402
from rag.domains import create_domain, delete_domain, get_domain, list_domains, rename_domain  # noqa: E402
from rag.cloud import CloudStorageError  # noqa: E402
from rag import user_auth, workspace  # noqa: E402

app = FastAPI(title="Query Assistant API", version="1.0")
app.include_router(user_auth.router)


@app.exception_handler(CloudStorageError)
async def storage_error_handler(request, exc):
    # A handled response passes through CORS so browsers can read the error.
    return JSONResponse(status_code=502, content={"detail": str(exc)})

@app.middleware("http")
async def require_user(request, call_next):
    owner_token = None
    # Protect every RAG route, including future routes and API documentation.
    if request.method != "OPTIONS":
        try:
            if not user_auth.configured():
                raise HTTPException(503, "Supabase authentication is not configured on the backend.")
            user_auth.check_origin(request)
            if request.url.path not in user_auth.PUBLIC_PATHS:
                await user_auth.authenticate(request)
                try:
                    owner_token = workspace.bind(request.state.user)
                except (ValueError, KeyError, TypeError):
                    raise HTTPException(401, "Invalid account identity. Please sign in again.") from None
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers={"Cache-Control": "no-store"})
    try:
        response = await call_next(request)
    finally:
        if owner_token is not None:
            workspace.reset(owner_token)
    response.headers["Cache-Control"] = "no-store"
    return response


# CORS runs outside authentication so preflight and login errors remain readable.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
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
@app.get("/")
@app.get("/api/health")
def health():
    errors = config.configuration_errors()
    return {"ok": not errors, "configuration_errors": errors, "storage": "supabase" if config.CLOUD_MODE else "local",
            "deepseek_key": bool(config.DEEPSEEK_API_KEY), "qdrant": "server" if config.QDRANT_URL else "local"}


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


@app.delete("/api/domains/{key}/documents/{file_hash}")
def remove_document(key: str, file_hash: str):
    _domain(key)
    try:
        ingest.delete_document(key, file_hash)
    except KeyError as exc:
        raise HTTPException(404, "Document not found in this knowledge base.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


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
    if config.CLOUD_MODE:
        raise HTTPException(400, "Delete individual PDFs and upload again to rebuild this cloud knowledge base.")
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

    if config.CLOUD_MODE:
        from rag import cloud
        try:
            if file is not None and file.filename:
                if Path(file.filename).suffix.lower() not in extract.SUPPORTED_EXTENSIONS:
                    raise HTTPException(400, "Choose a PDF, JPG, JPEG, PNG, JFIF, WebP, BMP, TIF, TIFF or AVIF file.")
                content = await file.read(config.MAX_UPLOAD_BYTES + 1)
                if not content or len(content) > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Choose a non-empty PDF or image smaller than 4 MB.")
                name = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename)
                return {"jobs": [cloud.start(domain, name, content, use_llm=use_llm, force=force)]}
            jobs = [{"file_hash": row["file_hash"], "status": row["status"]}
                    for row in cloud.documents(key) if row["status"] != "ready"]
            if not jobs:
                raise HTTPException(404, "No pending saved PDFs. Upload a PDF to this cloud knowledge base.")
            return {"jobs": jobs}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    targets: list[Path]
    if file is not None and file.filename:
        domain.pdf_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename)
        if Path(safe).suffix.lower() not in extract.SUPPORTED_EXTENSIONS:
            raise HTTPException(400, "Choose a PDF, JPG, JPEG, PNG, JFIF, WebP, BMP, TIF, TIFF or AVIF file.")
        target = domain.pdf_dir / safe
        content = await file.read(config.MAX_UPLOAD_BYTES + 1)
        if len(content) > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Choose a document smaller than 4 MB.")
        try:
            extract.document_pdf(safe, content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        target.write_bytes(content)
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

    context = copy_context()
    threading.Thread(target=context.run, args=(work,), daemon=True).start()

    def stream():
        while True:
            item = events.get()
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/domains/{key}/documents/{file_hash}/pdf")
def preview_pdf(key: str, file_hash: str):
    domain = _domain(key)
    if not re.fullmatch(r"[a-f0-9]{64}", file_hash):
        raise HTTPException(400, "Invalid document identifier.")
    if config.CLOUD_MODE:
        from rag import cloud
        row = next((row for row in cloud.documents(key) if row["file_hash"] == file_hash), None)
        if not row:
            raise HTTPException(404, "Document not found in this knowledge base.")
        if Path(row["name"]).suffix.lower() != ".pdf":
            raise HTTPException(400, "PDF preview is available for PDF documents.")
        content = cloud.read_document(row["object_path"])
    else:
        row = ingest.list_ingested(key).get(file_hash)
        if not row:
            raise HTTPException(404, "Document not found in this knowledge base.")
        if Path(row["name"]).suffix.lower() != ".pdf":
            raise HTTPException(400, "PDF preview is available for PDF documents.")
        path = next((p for p in domain.default_pdfs()
                     if p.suffix.lower() == ".pdf" and extract.file_sha256(p) == file_hash), None)
        if path is None:
            raise HTTPException(404, "The original PDF is unavailable. Upload the PDF again.")
        content = path.read_bytes()
    return Response(content, media_type="application/pdf", headers={"Content-Disposition": "inline"})


@app.get("/api/domains/{key}/documents/{file_hash}/text")
def image_text(key: str, file_hash: str):
    domain = _domain(key)
    if not re.fullmatch(r"[a-f0-9]{64}", file_hash):
        raise HTTPException(400, "Invalid document identifier.")
    if config.CLOUD_MODE:
        from rag import cloud
        row = next((row for row in cloud.documents(key) if row["file_hash"] == file_hash), None)
        if not row:
            raise HTTPException(404, "Document not found in this knowledge base.")
        if Path(row["name"]).suffix.lower() not in extract.IMAGE_EXTENSIONS or row["status"] != "ready":
            raise HTTPException(400, "Process an image first to view its extracted text.")
        try:
            text = cloud.read_image_text(row["object_path"])
        except CloudStorageError as exc:
            raise HTTPException(404, "Extracted text is unavailable. For older images, delete and upload the image again. If storage is unavailable, retry shortly.") from exc
        return {"name": row["name"], "text": text}
    row = ingest.list_ingested(key).get(file_hash)
    if not row:
        raise HTTPException(404, "Document not found in this knowledge base.")
    if Path(row["name"]).suffix.lower() not in extract.IMAGE_EXTENSIONS:
        raise HTTPException(400, "Extracted text preview is available for images.")
    text = row.get("extracted_text")
    if text is None:
        # Older indexed images can be previewed without rebuilding their vectors.
        path = next((p for p in domain.default_pdfs() if extract.file_sha256(p) == file_hash), None)
        if path is None:
            raise HTTPException(404, "The original image is unavailable. Upload the image again.")
        try:
            pages = extract.extract_pdf(path)
            text = "\n\n".join(t for page in pages for t in page["image_text"])
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"name": row["name"], "text": text}


@app.post("/api/domains/{key}/documents/{file_hash}/step")
def process_page(key: str, file_hash: str):
    if not config.CLOUD_MODE:
        raise HTTPException(404, "Page jobs are only available in Supabase mode.")
    if not re.fullmatch(r"[a-f0-9]{64}", file_hash):
        raise HTTPException(400, "Invalid document identifier.")
    from rag import cloud
    try:
        return cloud.step(_domain(key), file_hash)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
