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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse, Response  # noqa: E402
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field  # noqa: E402

from rag import answer, config, ingest, extract  # noqa: E402
from rag.domains import create_domain, delete_domain, get_domain, list_domains, rename_domain  # noqa: E402
from rag.cloud import CloudStorageError  # noqa: E402
from rag import user_auth, workspace, admin_panel, saved_uploads, access_control, super_admin, security_store  # noqa: E402

app = FastAPI(title="Query Assistant API", version="1.0")
app.include_router(user_auth.router)
app.include_router(admin_panel.router)
app.include_router(super_admin.router)

ACTIVITY_ROUTES = {
    ("POST", "/api/super-admin/user-details"): ("user-details", "Created or invited User"),
    ("POST", "/api/super-admin/accounts"): ("user-details", "Created or invited account"),
    ("DELETE", "/api/super-admin/accounts/{owner}"): ("user-details", "Permanently deleted account"),
    ("PUT", "/api/super-admin/original-admin/status"): ("user-details", "Updated original Admin access status"),
    ("PUT", "/api/admin/users/{user_id}/permissions"): ("user-details", "Updated account upload policy"),
    ("PUT", "/api/super-admin/accounts/{owner}/upload-approval"): ("role-management", "Updated upload approval access"),
    ("PUT", "/api/super-admin/roles/admin"): ("role-management", "Updated Admin role status"),
    ("POST", "/api/domains"): ("knowledge-base", "Created knowledge base"),
    ("PATCH", "/api/domains/{key}"): ("knowledge-base", "Renamed knowledge base"),
    ("DELETE", "/api/domains/{key}"): ("knowledge-base", "Deleted knowledge base"),
    ("POST", "/api/domains/{key}/reset"): ("knowledge-base", "Reset knowledge base"),
    ("POST", "/api/domains/{key}/ingest"): ("knowledge-base", "Started document upload or processing"),
    ("PUT", "/api/domains/{key}/documents/{file_hash}/chunks"): ("knowledge-base", "Edited document text"),
    ("DELETE", "/api/domains/{key}/documents/{file_hash}"): ("knowledge-base", "Deleted document"),
    ("POST", "/api/uploads"): ("knowledge-base", "Submitted file for upload"),
    ("POST", "/api/upload-requests"): ("knowledge-base", "Submitted upload request"),
    ("PUT", "/api/admin/upload-requests/{request_id}"): ("knowledge-base", "Reviewed upload request"),
    ("DELETE", "/api/admin/upload-requests/{request_id}"): ("knowledge-base", "Removed upload request"),
    ("POST", "/api/admin/upload-requests/for/{owner}"): ("knowledge-base", "Submitted file for another account"),
}


def log_request_activity(request, owner):
    route = getattr(request.scope.get("route"), "path", None)
    event = ACTIVITY_ROUTES.get((request.method, route))
    events = getattr(request.state, "activity_events", None) or ([event] if event else [])
    if not events:
        return
    params = request.path_params
    target = getattr(request.state, "activity_target", None) or next(iter(params.values()), None)
    if events[0][0] == "knowledge-base" and "owner" not in params and "request_id" not in params:
        parts = (owner, target, params.get("file_hash"))
        target = " / ".join(part for part in parts if part)
    if not target and "original-admin" in route:
        target = "local-admin"
    if not target and route.endswith("/roles/admin"):
        target = "admin"
    for category, action in events:
        security_store.record_activity(category, request.state.user["id"], target, action)


@app.exception_handler(CloudStorageError)
async def storage_error_handler(request, exc):
    # A handled response passes through CORS so browsers can read the error.
    return JSONResponse(status_code=502, content={"detail": str(exc)})

@app.middleware("http")
async def require_user(request, call_next):
    owner_token = actor_token = None
    writer = None
    # Protect every RAG route, including future routes and API documentation.
    if request.method != "OPTIONS":
        try:
            if not user_auth.configured():
                raise HTTPException(503, "Supabase authentication is not configured on the backend.")
            user_auth.check_origin(request)
            if request.url.path not in user_auth.PUBLIC_PATHS:
                await user_auth.authenticate(request)
                try:
                    access_control.authorize(request.state.user, request.url.path, request.method)
                    identity = (await run_in_threadpool(super_admin.selected_workspace, request)
                                if request.headers.get("x-workspace-account") else request.state.user)
                    owner_token = workspace.bind(identity)
                    path = request.url.path
                    if (request.method in {"DELETE", "PATCH", "POST", "PUT"}
                            and path.startswith(("/api/domains", "/api/uploads", "/api/upload-requests"))):
                        owner = identity["id"]
                        if not workspace.begin_write(owner):
                            workspace.reset(owner_token)
                            raise HTTPException(409, "This workspace is being deleted.")
                        writer = owner
                    actor_token = workspace.bind_actor(request.state.user)
                except (ValueError, KeyError, TypeError):
                    raise HTTPException(401, "Invalid account identity. Please sign in again.") from None
        except CloudStorageError as exc:
            return await storage_error_handler(request, exc)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers={"Cache-Control": "no-store"})
    try:
        response = await call_next(request)
    finally:
        if writer is not None:
            workspace.end_write(writer)
        if actor_token is not None:
            workspace.reset_actor(actor_token)
        if owner_token is not None:
            workspace.reset(owner_token)
    if actor_token is not None and request.method in {"POST", "PUT", "PATCH", "DELETE"} and 200 <= response.status_code < 300:
        await run_in_threadpool(log_request_activity, request, identity["id"])
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
def new_domain(body: DomainBody, request: Request):
    try:
        domain = create_domain(body.name)
        request.state.activity_target = domain.key
        return domain_info(domain)
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
    request: Request,
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

    delegated_upload = request.state.user["role"] != "super_admin" and request.headers.get("x-workspace-account")
    if delegated_upload and (force or file is None or not file.filename):
        raise HTTPException(400, "Select a PDF or image to add to this user workspace.")

    if force and not access_control.allows(request.state.user, "documents.edit"):
        raise HTTPException(403, "Editing documents is disabled for your account.")

    if not delegated_upload and workspace.owner_id() == "admin" and request.state.user.get("direct_upload_restricted"):
        raise HTTPException(403, "Direct uploads are disabled for this account.")

    # Enforce approval before storage, extraction, embedding or processing.
    if file is not None and file.filename:
        approval_content = await file.read(config.MAX_UPLOAD_BYTES + 1)
        if not approval_content or len(approval_content) > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Choose a non-empty PDF or image smaller than 4 MB.")
        admin_panel.require_upload(key, file.filename, approval_content,
                                   direct_upload=access_control.allows_direct_upload(request.state.user))
        await file.seek(0)
    elif not workspace.privileged_actor() and workspace.owner_id() != "admin" and admin_panel.policy(workspace.owner_id()) == "blocked":
        raise HTTPException(403, "Uploads are blocked for your account.")

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
        if delegated_upload:
            try:
                with target.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                raise HTTPException(409, "A file with this name already exists in the user workspace.") from None
        else:
            target.write_bytes(content)
        targets = [target]
    else:
        targets = domain.default_pdfs()
        if not targets:
            raise HTTPException(404, f"No bundled PDF found in pdfs/{domain.pdf_subdir}/. Upload one instead.")

    events: "queue.Queue[dict | None]" = queue.Queue()
    writer = "local-admin" if workspace.owner_id() == "admin" else workspace.owner_id()
    if not workspace.begin_write(writer):
        raise HTTPException(409, "This workspace is being deleted.")

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
            workspace.end_write(writer)
            events.put(None)

    context = copy_context()
    try:
        threading.Thread(target=context.run, args=(work,), daemon=True).start()
    except Exception:
        workspace.end_write(writer)
        raise

    def stream():
        while True:
            item = events.get()
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class EditedChunk(BaseModel):
    text: str = Field(max_length=100000)
    page: int = Field(ge=1)


class DocumentEdit(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    chunks: list[EditedChunk] = Field(max_length=1000)


@app.get("/api/domains/{key}/documents/{file_hash}/chunks")
def document_chunks(key: str, file_hash: str):
    from rag import document_editor
    domain = _domain(key)
    try:
        return document_editor.read(domain, file_hash)
    except KeyError:
        raise HTTPException(404, "Document not found.")
    except document_editor.EditConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/domains/{key}/documents/{file_hash}/chunks")
def save_document_chunks(key: str, file_hash: str, body: DocumentEdit):
    from rag import document_editor
    domain = _domain(key)
    try:
        return document_editor.save(domain, file_hash, body.revision,
                                    [part.model_dump() for part in body.chunks])
    except KeyError:
        raise HTTPException(404, "Document not found.")
    except document_editor.EditConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
    if not re.fullmatch(r"[a-f0-9]{64}", file_hash):
        raise HTTPException(400, "Invalid document identifier.")
    saved = saved_uploads.find(key,file_hash)
    if saved:
        return saved_uploads.process_step(saved)
    if not config.CLOUD_MODE:
        raise HTTPException(404, "Page jobs are only available in Supabase mode.")
    from rag import cloud
    domain = _domain(key)
    try:
        return cloud.step(domain, file_hash)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
