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

_OCR_WARNED = False


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
    """OCR one PIL image. Returns '' (and warns once) if Tesseract is not installed.

    psm 6 (one uniform block, row by row) reads tables and infographics best; whole scanned
    pages use psm 3 (automatic layout).
    """
    global _OCR_WARNED
    try:
        import pytesseract

        cmd = config.tesseract_cmd()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        prepared = _prepare_for_ocr(img)
        text = pytesseract.image_to_string(prepared, lang=config.OCR_LANG, config=f"--psm {psm or config.OCR_PSM}")
        if len([l for l in text.splitlines() if l.strip()]) < 3 and psm is None:
            # very little text found: sparse layouts (icons + labels, infographics) read better with psm 12
            retry = pytesseract.image_to_string(prepared, lang=config.OCR_LANG, config="--psm 12")
            if len(retry.strip()) > len(text.strip()):
                text = retry
    except Exception as exc:  # Tesseract missing / language data missing
        if not _OCR_WARNED:
            warnings.warn(
                f"OCR is not available ({exc}). Install Tesseract and set TESSERACT_CMD in .env. "
                "Text inside images will be skipped.",
                stacklevel=2,
            )
            _OCR_WARNED = True
        return ""
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
    cache_file = config.CACHE_DIR / f"{digest}.raw.json"
    if use_cache and cache_file.exists():
        if progress:
            progress("extraction loaded from cache")
        return json.loads(cache_file.read_text(encoding="utf-8"))

    pages: list[dict] = []
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
