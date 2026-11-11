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
from .ocr_refine import refine_ocr as _refine_ocr


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _prepare_for_ocr(img: Image.Image) -> Image.Image:
    """Conservative baseline, retained so enhancement cannot erase clear text."""
    img = ImageOps.exif_transpose(img).convert("RGBA")
    background = Image.new("RGBA", img.size, "white")
    background.alpha_composite(img)
    img = background.convert("L")
    # Bound both magnification and total working pixels for tall/narrow images.
    scale = min(max(1.0, 1800 / img.width), 4.0,
                (12_000_000 / (img.width * img.height)) ** 0.5)
    if scale != 1:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    return ImageOps.autocontrast(img)


def _deskew(img: Image.Image) -> Image.Image:
    """Correct small scan rotations only when row alignment clearly improves."""
    import numpy as np

    preview = img.copy()
    preview.thumbnail((700, 700))
    pixels = np.asarray(preview)
    mask = Image.fromarray((pixels < min(180, float(pixels.mean()) - 25)).astype("uint8") * 255)
    if np.count_nonzero(mask) < 40:
        return img

    def score(angle):
        rows = np.asarray(mask.rotate(angle, resample=Image.Resampling.NEAREST, expand=True)).sum(axis=1, dtype=float)
        return float(np.square(np.diff(rows)).sum())

    baseline = score(0)
    coarse = max(range(-8, 9), key=score)
    angles = [coarse + offset / 4 for offset in range(-3, 4) if abs(coarse + offset / 4) <= 8]
    angle = max(angles, key=score)
    if abs(angle) < 0.5 or score(angle) < baseline * 1.20:
        return img
    return img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)


def _ocr_variants(img: Image.Image):
    """Generate expensive alternatives lazily, only after a weak baseline."""
    import numpy as np
    from PIL import ImageFilter

    enhanced = _deskew(img.filter(ImageFilter.MedianFilter(3)))
    enhanced = ImageOps.autocontrast(enhanced, cutoff=1).filter(
        ImageFilter.UnsharpMask(radius=1.5, percent=160, threshold=3))
    yield enhanced
    # Local background normalization handles shadows and uneven illumination.
    background = enhanced.filter(ImageFilter.GaussianBlur(15))
    values = np.asarray(enhanced, dtype="float32")
    local = np.asarray(background, dtype="float32")
    yield Image.fromarray(np.where(values < local - 10, 0, 255).astype("uint8"))


class _OCRResult(tuple):
    """Keep internal positions alongside the existing four-value OCR score."""
    def __new__(cls, values, data, image):
        result = super().__new__(cls, values)
        result.data, result.image = data, image
        return result


def _read_ocr(img: Image.Image, psm: int):
    """Get text and character-weighted word confidence from one engine run."""
    import pytesseract

    data = pytesseract.image_to_data(img, lang=config.OCR_LANG,
        config=f"--psm {psm}", output_type=pytesseract.Output.DICT, timeout=60)
    lines = {}
    weighted, characters, reliable = 0.0, 0, 0
    for i, word in enumerate(data["text"]):
        word = word.strip()
        confidence = float(data["conf"][i])
        if not word or confidence < 0:
            continue
        key = tuple(data[field][i] for field in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(word)
        length = len(word)
        weighted += confidence * length
        characters += length
        if confidence >= 60:
            reliable += length
    text = "\n".join(" ".join(words) for words in lines.values())
    confidence = weighted / characters if characters else 0.0
    # Confidence alone can prefer one recognised word over an entire page.
    quality = confidence * (reliable / characters if characters else 0) * min(1.0, characters / 20)
    return _OCRResult((text, confidence, quality, characters), data, img)


def _prefer_ocr(candidate, current) -> bool:
    """Discount confidence gains obtained by dropping recognised content.

    Segmentation can silently omit a difficult footer while raising average word
    confidence. Compare candidates at a shared character coverage before replacing
    an existing result. Never prefer output merely because it is longer.
    """
    coverage = min(1.0, candidate[3] / max(current[3], 1))
    return candidate[2] * coverage > current[2] + 2


def ocr_image(img: Image.Image, psm: int | None = None) -> str:
    """Adaptive local OCR; preserve the baseline unless a retry scores better.

    At most six page OCR calls plus eight targeted line calls. Explicit layout
    modes participate in preprocessing retries; sparse layout is only tried
    when the baseline is weak.
    """
    try:
        import pytesseract

        cmd = config.tesseract_cmd()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        prepared = _prepare_for_ocr(img)
        mode = config.OCR_PSM if psm is None else psm
        best = _read_ocr(prepared, mode)
        if best[2] < 85 or best[3] < 20:
            candidates = ((prepared, 12),)
            # Retrying a candidate must not discard a successful baseline on timeout.
            for candidate, layout in candidates:
                if layout == mode:
                    continue
                try:
                    result = _read_ocr(candidate, layout)
                except RuntimeError:
                    continue
                if _prefer_ocr(result, best):
                    best = result
            for candidate in _ocr_variants(prepared):
                for layout in dict.fromkeys((mode, 12)):
                    try:
                        result = _read_ocr(candidate, layout)
                    except RuntimeError:
                        continue
                    if _prefer_ocr(result, best):
                        best = result
                    if best[2] >= 90 and best[3] >= 20:
                        return _refine_ocr(best)
        return _refine_ocr(best)
    except Exception as exc:
        raise RuntimeError(
            "OCR failed. Install Tesseract and requirements-local.txt on the backend, "
            "check TESSERACT_CMD and OCR_LANG, then reprocess this document."
        ) from exc


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
    cache_file = config.CACHE_DIR / f"{digest}.ocr-v5.raw.json"
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
