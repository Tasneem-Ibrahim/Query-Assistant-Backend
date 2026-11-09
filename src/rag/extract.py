"""STEP 1-3 of the diagram: extract text, detect images, OCR the text inside images.

For every PDF page we produce two separate pieces of data:
    Data01 = text layer of the page             (PyMuPDF)
    Data02 = text found INSIDE images on page   (Tesseract OCR, only if is_image)
The LLM later merges them and removes duplicates (see clean.py).
"""

from __future__ import annotations

import hashlib
import io
import json
import warnings
from pathlib import Path
from typing import Callable

import pymupdf
from PIL import Image, ImageOps

from . import config


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _prepare_for_ocr(img: Image.Image) -> Image.Image:
    """Grayscale + upscale + contrast: small, coloured table images OCR much better this way."""
    img = img.convert("RGB").convert("L")
    if img.width < 1800:
        scale = 1800 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(img)


def ocr_image(img: Image.Image, psm: int | None = None) -> str:
    """OCR one PIL image. Raises a clear error when the OCR engine is unavailable.

    psm 6 (one uniform block, row by row) reads tables and infographics best; whole scanned
    pages use psm 3 (automatic layout).
    """
    try:
        import pytesseract

        cmd = config.tesseract_cmd()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        prepared = _prepare_for_ocr(img)
        text = pytesseract.image_to_string(prepared, lang=config.OCR_LANG, config=f"--psm {psm or config.OCR_PSM}", timeout=60)
        if len([l for l in text.splitlines() if l.strip()]) < 3 and psm is None:
            # very little text found: sparse layouts (icons + labels, infographics) read better with psm 12
            retry = pytesseract.image_to_string(prepared, lang=config.OCR_LANG, config="--psm 12", timeout=60)
            if len(retry.strip()) > len(text.strip()):
                text = retry
    except Exception as exc:  # Tesseract missing / language data missing
        raise RuntimeError(
            "OCR failed. Install Tesseract and requirements-local.txt on the backend, "
            "check TESSERACT_CMD and OCR_LANG, then reprocess this document."
        ) from exc
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def page_images(doc: pymupdf.Document, page: pymupdf.Page) -> list[Image.Image]:
    """All meaningful images on a page (tiny icons/logos are skipped, repeats counted once)."""
    images, seen = [], set()
    for info in page.get_images(full=True):
        xref = info[0]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            data = doc.extract_image(xref)
            img = Image.open(io.BytesIO(data["image"]))
            img.load()
        except Exception:
            continue
        if min(img.size) < config.MIN_IMAGE_PIXELS:
            continue
        images.append(img)
    return images


def extract_pdf(
    pdf_path: Path,
    use_cache: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Return one dict per page: {page, text, is_image, image_text, scanned}."""
    config.ensure_dirs()
    pdf_path = Path(pdf_path)
    digest = file_sha256(pdf_path)
    cache_file = config.CACHE_DIR / f"{digest}.ocr-v2.raw.json"
    if use_cache and cache_file.exists():
        if progress:
            progress("extraction loaded from cache")
        return json.loads(cache_file.read_text(encoding="utf-8"))

    pages: list[dict] = []
    if pdf_path.suffix.lower() in IMAGE_EXTENSIONS:
        for number, img in enumerate(image_frames(pdf_path.read_bytes()), start=1):
            text = ocr_image(img, psm=3)
            pages.append({"page": number, "text": "", "is_image": True,
                          "image_text": [text] if text.strip() else [], "scanned": True})
            if progress:
                progress(f"image page {number}: {len(text)} OCR characters")
        if not any(p["image_text"] for p in pages):
            raise ValueError("No readable text found in this image. Use a clearer image containing printed text.")
        cache_file.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
        return pages
    with pymupdf.open(pdf_path) as doc:
        for number, page in enumerate(doc, start=1):
            # 1. extract text
            text = page.get_text("text").strip()
            # 2. is_image
            images = page_images(doc, page)
            is_image = bool(images)
            image_text: list[str] = []
            scanned = False
            # 3. is_image == True -> extract the text inside the images
            if is_image:
                for img in images:
                    found = ocr_image(img)
                    if found:
                        image_text.append(found)
            elif not text:
                # no text layer and no embedded image: a scanned page -> OCR the whole page
                pix = page.get_pixmap(dpi=200)
                full_page = Image.open(io.BytesIO(pix.tobytes("png")))
                found = ocr_image(full_page, psm=3)
                if found:
                    image_text.append(found)
                    is_image, scanned = True, True
            pages.append(
                {"page": number, "text": text, "is_image": is_image, "image_text": image_text, "scanned": scanned}
            )
            if progress:
                progress(f"page {number}/{len(doc)}: {len(text)} text chars, {len(image_text)} image(s) OCR'd")

    cache_file.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    return pages


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".jfif", ".webp", ".bmp", ".tif", ".tiff", ".avif"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}


def document_pdf(name: str, content: bytes) -> bytes:
    """Validate uploads; normalize images to PDFs for cloud storage."""
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("Choose a PDF, JPG, JPEG, PNG, JFIF, WebP, BMP, TIF, TIFF or AVIF file.")
    if not content:
        raise ValueError("This file is empty.")
    if suffix == ".pdf":
        try:
            with pymupdf.open(stream=content, filetype="pdf") as doc:
                if doc.is_encrypted or not 1 <= len(doc) <= 500:
                    raise ValueError("Use an unlocked PDF with 1-500 pages.")
        except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
            raise ValueError("This file is not a readable PDF.") from exc
        return content
    with pymupdf.open() as doc:
        for img in image_frames(content):
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            page = doc.new_page(width=img.width * 72 / 200, height=img.height * 72 / 200)
            page.insert_image(page.rect, stream=buffer.getvalue())
        return doc.tobytes(deflate=True)


def image_frames(content: bytes):
    """Decode supported images, preserving TIFF pages and flattening alpha on white.

    Animated WebP/AVIF/PNG use the first frame. TIFF pages are separate documents
    pages. Bound the decoded total independently of the compressed upload size.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP", "BMP", "TIFF", "AVIF"}:
                    raise ValueError("Upload a supported JPEG, PNG, JFIF, WebP, BMP, TIFF or AVIF image.")
                count = getattr(source, "n_frames", 1) if source.format == "TIFF" else 1
                if not 1 <= count <= 500:
                    raise ValueError("Use an image with 1-500 pages.")
                total_pixels = 0
                for index in range(count):
                    source.seek(index)
                    total_pixels += source.width * source.height
                    if total_pixels > 50_000_000:
                        raise ValueError("Image is too large after decoding. Split it or reduce its resolution.")
                    source.load()
                    frame = ImageOps.exif_transpose(source.copy()).convert("RGBA")
                    background = Image.new("RGBA", frame.size, "white")
                    background.alpha_composite(frame)
                    yield background.convert("RGB")
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("This image is unreadable, unsupported by the installed decoder, or too large. Use a smaller valid image and Pillow 11.3 or newer.") from exc
