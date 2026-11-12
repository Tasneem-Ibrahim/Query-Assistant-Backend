"""Supabase persistence and resumable, one-page-at-a-time cloud ingestion."""
from __future__ import annotations

import base64
import atexit
import hashlib
import logging
import uuid
from urllib.parse import quote

import httpx
import pymupdf

from . import config, workspace


class CloudStorageError(RuntimeError):
    """Safe public message for a failed storage dependency."""


# Reuse TCP connections across requests; retry only connection establishment.
_client = httpx.Client(timeout=60, transport=httpx.HTTPTransport(
    retries=2, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)))
atexit.register(_client.close)


def _request(method, path, **kwargs):
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        raise ValueError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY on the backend.")
    headers = {"apikey": config.SUPABASE_SERVICE_ROLE_KEY,
               "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}"}
    headers.update(kwargs.pop("headers", {}))
    try:
        response = _client.request(method, config.SUPABASE_URL + path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise CloudStorageError("Cannot reach Supabase. Check the backend connection and SUPABASE_URL, then retry.") from exc
    if not response.is_success:
        # Never pass provider bodies/URLs (which can contain credentials) to browsers.
        raise CloudStorageError(f"Supabase request failed (HTTP {response.status_code}). Check the SQL setup, private bucket and service-role key.")
    return response


def rows(table, **filters):
    result, offset = [], 0
    while True:
        page = _request("GET", "/rest/v1/" + table, params={**filters, "limit": 1000, "offset": offset}).json()
        result.extend(page)
        if len(page) < 1000:
            return result
        offset += len(page)


def write(table, data, conflict):
    return _request("POST", "/rest/v1/" + table, params={"on_conflict": conflict},
                    headers={"Prefer": "resolution=merge-duplicates,return=representation"}, json=data).json()


def remove(table, **filters):
    _request("DELETE", "/rest/v1/" + table, params=filters)


def scope(domain):
    return {"namespace": "eq." + config.embedding_namespace(), "domain": "eq." + domain}


def documents(domain):
    return rows("qa_documents", **scope(domain), order="file_hash.asc")


def report(row):
    return {key: row[key] for key in ("name", "pages", "chunks", "image_pages")}


def _release(args):
    try:
        _request("POST", "/rest/v1/rpc/qa_release_document", json=args)
    except RuntimeError:
        # A committed page/delete remains successful; uncommitted leases expire.
        logging.getLogger(__name__).warning("Could not release processing lease; it will expire automatically.")


def list_ingested(domain):
    return {row["file_hash"]: report(row) for row in documents(domain) if row["status"] == "ready"}


def _object_path(path):
    # Even service-role object reads must stay inside the verified workspace.
    parts = path.split("/")
    if len(parts) != 3 or not workspace.owns_namespace(parts[0]) or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Document does not belong to this workspace.")
    return "/storage/v1/object/" + quote(config.SUPABASE_BUCKET, safe="") + "/" + quote(path, safe="/")


def _save_image_text(object_path, text, page=1):
    """Keep exact UTF-8 OCR in a PDF attachment; the bucket accepts PDFs only."""
    previous = _request("GET", _object_path(object_path + ".ocr.pdf")).content if page > 1 else None
    attachments = {}
    if previous:
        with pymupdf.open(stream=previous, filetype="pdf") as saved:
            attachments = {name: saved.embfile_get(name) for name in saved.embfile_names()}
    attachments[f"ocr-{page:04d}.txt"] = text.encode("utf-8")
    with pymupdf.open() as preview:
        preview.new_page()
        for name, data in sorted(attachments.items()):
            preview.embfile_add(name, data)
        content = preview.tobytes()
    _request("POST", _object_path(object_path + ".ocr.pdf"), content=content,
             headers={"Content-Type": "application/pdf", "x-upsert": "true"})


def read_document(object_path):
    return _request("GET", _object_path(object_path)).content


def read_image_text(object_path):
    response = _request("GET", _object_path(object_path + ".ocr.pdf"))
    try:
        with pymupdf.open(stream=response.content, filetype="pdf") as preview:
            names = sorted(name for name in preview.embfile_names() if name.startswith("ocr-") and name.endswith(".txt"))
            if not names:
                return preview.embfile_get("ocr.txt").decode("utf-8")
            texts = [preview.embfile_get(name).decode("utf-8") for name in names]
            return "\n\n".join(f"Page {index}\n{text}" for index, text in enumerate(texts, 1)) if len(texts) > 1 else texts[0]
    except (ValueError, RuntimeError, UnicodeError) as exc:
        raise CloudStorageError("Saved OCR preview is unreadable. Reprocess this image.") from exc


def start(domain, name, content, use_llm=True, force=False):
    errors = config.configuration_errors()
    if errors:
        raise ValueError("; ".join(errors))
    from .extract import document_pdf
    normalized = document_pdf(name, content)
    digest = hashlib.sha256(content).hexdigest()
    existing = next((row for row in documents(domain.key) if row["file_hash"] == digest), None)
    if existing:
        if force:
            raise ValueError("Delete this document first, then upload it again to rebuild it.")
        return {"file_hash": digest, "status": existing["status"], "report": report(existing)}
    try:
        with pymupdf.open(stream=normalized, filetype="pdf") as pdf:
            if pdf.is_encrypted or not 1 <= len(pdf) <= 500:
                raise ValueError("Use an unlocked PDF with 1–500 pages.")
            pages = len(pdf)
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
        raise ValueError("This file is not a readable PDF.") from exc
    path = f"{config.embedding_namespace()}/{domain.key}/{digest}.pdf"
    _request("POST", _object_path(path), content=normalized,
             headers={"Content-Type": "application/pdf", "x-upsert": "true"})
    row = {"namespace": config.embedding_namespace(), "domain": domain.key,
           "file_hash": digest, "name": name, "object_path": path, "pages": pages,
           "use_llm": use_llm}
    # A simultaneous upload must not reset another request's page cursor.
    _request("POST", "/rest/v1/qa_documents", params={"on_conflict": "namespace,domain,file_hash"},
             headers={"Prefer": "resolution=ignore-duplicates"}, json=row)
    return {"file_hash": digest, "status": "pending"}


def _ocr_page(pdf, page_index):
    import os
    if os.getenv("OCR_PROVIDER", "api") == "tesseract":
        from .extract import ocr_image
        from PIL import Image
        import io
        pix = pdf[page_index].get_pixmap(dpi=200)
        with Image.open(io.BytesIO(pix.tobytes("png"))) as img:
            text = ocr_image(img, psm=3)
        if not text.strip():
            raise ValueError("No readable text found on this page. Use a clearer scan or image.")
        return text
    if not config.OCR_API_KEY:
        raise ValueError("This page needs OCR. Set OCR_API_KEY for cloud OCR, or set OCR_PROVIDER=tesseract on a backend with Tesseract installed, then resume processing.")
    with pymupdf.open() as single:
        single.insert_pdf(pdf, from_page=page_index, to_page=page_index)
        data = base64.b64encode(single.tobytes()).decode()
    response = httpx.post(config.OCR_API_URL,
        headers={"Authorization": f"Bearer {config.OCR_API_KEY}"},
        json={"model": config.OCR_API_MODEL,
              "document": {"type": "document_url", "document_url": "data:application/pdf;base64," + data}}, timeout=60)
    if not response.is_success:
        raise RuntimeError(f"OCR provider returned HTTP {response.status_code}. Check its key and quota, then resume.")
    text = "\n".join(page["markdown"] for page in response.json()["pages"])
    if not text.strip():
        raise ValueError("OCR returned no text for this page. Check the PDF before retrying.")
    return text


def step(domain, digest):
    from . import chunk, clean, embed, vectorstore

    token = str(uuid.uuid4())
    args = {"p_namespace": config.embedding_namespace(), "p_domain": domain.key,
            "p_hash": digest, "p_token": token}
    row = _request("POST", "/rest/v1/rpc/qa_claim_document", json=args).json()
    if not row:
        raise ValueError("Document not found, or another request is processing it. Retry shortly.")
    if row["status"] == "ready":
        return {"status": "ready", "report": report(row)}
    try:
        index = row["next_page"]
        chunks = row.get("cached_chunks")
        image = row.get("cached_image", False)
        if chunks is None:
            content = _request("GET", _object_path(row["object_path"])).content
            with pymupdf.open(stream=content, filetype="pdf") as pdf:
                page = pdf[index]
                image = bool(page.get_images()) or not page.get_text().strip()
                # OCR the whole page once, avoiding duplicate text/image extraction.
                text = _ocr_page(pdf, index) if image else page.get_text().strip()
            from .extract import IMAGE_EXTENSIONS
            from pathlib import Path
            if Path(row["name"]).suffix.lower() in IMAGE_EXTENSIONS:
                _save_image_text(row["object_path"], text, page=index + 1)
            raw = {"page": index + 1, "text": text, "image_text": [], "is_image": image}
            cleaned = clean.clean_pages([raw], use_llm=row["use_llm"])
            chunks = chunk.chunk_pages(cleaned, domain.key, row["name"], digest)
            saved = _request("PATCH", "/rest/v1/qa_documents",
                params={**scope(domain.key), "file_hash": "eq." + digest, "lease_token": "eq." + token},
                headers={"Prefer": "return=representation"},
                json={"cached_chunks": chunks, "cached_image": image}).json()
            if not saved:
                raise RuntimeError("Processing lease expired. Resume this document to retry.")
        if chunks:
            embedder = embed.get_embedder()
            vectors = embedder.embed_passages([part["text"] for part in chunks])
            chunks, vectors, _ = embed.drop_near_duplicates(chunks, vectors)
            # Keep stable page chunks on retries, including after a partial upsert.
            vectorstore.ensure_collection(domain.collection, embedder.dim)
            vectorstore.upsert(domain.collection, chunks, vectors)
        result = _request("POST", "/rest/v1/rpc/qa_advance_document", json={**args,
            "p_chunks": len(chunks), "p_image": int(image)}).json()
        if not result:
            raise RuntimeError("Processing lease expired. Resume this document to retry the page.")
        return {"status": result["status"], "message": f"{row['name']}: page {index + 1}/{row['pages']} processed.",
                "report": report(result)}
    finally:
        _release(args)


def delete_document(domain, digest):
    from . import vectorstore

    found = next((row for row in documents(domain.key) if row["file_hash"] == digest), None)
    if not found:
        raise KeyError(digest)
    _object_path(found["object_path"])
    # SQL claim prevents deleting a PDF currently being processed.
    token = str(uuid.uuid4())
    args = {"p_namespace": config.embedding_namespace(), "p_domain": domain.key, "p_hash": digest, "p_token": token}
    claimed = _request("POST", "/rest/v1/rpc/qa_claim_document", json={**args, "p_delete": True}).json()
    if not claimed:
        raise ValueError("Document is being processed. Wait for processing to finish before deleting it.")
    try:
        vectorstore.delete_file(domain.collection, digest)
        # Namespace-qualified object keys allow another embedding version to coexist.
        _request("DELETE", "/storage/v1/object/" + quote(config.SUPABASE_BUCKET, safe=""), json={"prefixes": [found["object_path"], found["object_path"] + ".ocr.pdf", found["object_path"] + ".ocr.txt"]})
        remove("qa_documents", **scope(domain.key), file_hash="eq." + digest, lease_token="eq." + token)
    finally:
        _release(args)
