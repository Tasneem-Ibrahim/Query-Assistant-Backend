"""Image upload validation and real local OCR without external services."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rag import config, extract


def image_bytes():
    image = Image.new("RGB", (1600, 500), "white")
    font = ImageFont.load_default(size=60)
    ImageDraw.Draw(image).text((60, 130), "Returns accepted within thirty days.", font=font, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ImageOCRTests(unittest.TestCase):
    def test_cloud_normalization_preserves_image_and_single_page(self):
        with pymupdf.open(stream=extract.document_pdf("notes.PNG", image_bytes()), filetype="pdf") as pdf:
            self.assertEqual(len(pdf), 1)
            self.assertTrue(pdf[0].get_images())

    @unittest.skipUnless(config.tesseract_cmd(), "Tesseract is not installed")
    def test_all_requested_formats_decode_normalize_and_extract_real_text(self):
        formats = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "jfif": "JPEG", "webp": "WEBP",
                   "bmp": "BMP", "tif": "TIFF", "tiff": "TIFF", "avif": "AVIF"}
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(config, DATA_DIR=root, CACHE_DIR=root / "cache"):
                for extension, format_name in formats.items():
                    with self.subTest(extension=extension):
                        buffer = io.BytesIO()
                        with Image.open(io.BytesIO(image_bytes())) as image:
                            image.convert("RGB").save(buffer, format=format_name)
                        path = root / ("notes." + extension)
                        path.write_bytes(buffer.getvalue())
                        with pymupdf.open(stream=extract.document_pdf(path.name, buffer.getvalue()), filetype="pdf") as pdf:
                            self.assertEqual(len(pdf), 1)
                            self.assertTrue(pdf[0].get_images())
                        pages = extract.extract_pdf(path, use_cache=False)
                        self.assertIn("thirty days", " ".join(pages[0]["image_text"]).lower())

    def test_multipage_tiff_preserves_all_pages_and_transparent_background(self):
        first = Image.new("RGB", (400, 200), "white")
        second = Image.new("RGB", (400, 200), "gray")
        buffer = io.BytesIO()
        first.save(buffer, format="TIFF", save_all=True, append_images=[second])
        with pymupdf.open(stream=extract.document_pdf("notes.tiff", buffer.getvalue()), filetype="pdf") as pdf:
            self.assertEqual(len(pdf), 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "notes.tiff"
            path.write_bytes(buffer.getvalue())
            with patch.multiple(config, DATA_DIR=root, CACHE_DIR=root / "cache"), patch.object(extract, "ocr_image", side_effect=["First page", "Second page"]):
                pages = extract.extract_pdf(path, use_cache=False)
                self.assertEqual([page["image_text"] for page in pages], [["First page"], ["Second page"]])
        buffer = io.BytesIO()
        Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(buffer, format="PNG")
        self.assertEqual(next(extract.image_frames(buffer.getvalue())).getpixel((0, 0)), (255, 255, 255))

    def test_reject_invalid_and_disguised_uploads(self):
        for name, content in [("bad.png", b"not an image"), ("bad.pdf", b"not a pdf"),
                              ("bad.exe", image_bytes()), ("empty.jpg", b"")]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                extract.document_pdf(name, content)

    def test_missing_engine_is_reported(self):
        with patch.object(config, "tesseract_cmd", return_value="missing-tesseract-executable"):
            with self.assertRaisesRegex(RuntimeError, "OCR failed"):
                extract.ocr_image(Image.new("RGB", (100, 100)))

    def test_local_image_ingestion_and_retrieval(self):
        from rag import embed, ingest, vectorstore
        from rag.domains import Domain
        import numpy as np
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = dict(CLOUD_MODE=False, DATA_DIR=root, CACHE_DIR=root / "cache",
                            REGISTRY_FILE=root / "registry.json", QDRANT_PATH=root / "qdrant", QDRANT_URL="")
            with patch.multiple(config, **settings):
                vectorstore.close_client()
                try:
                    image = root / "returns.png"
                    image.write_bytes(image_bytes())
                    domain = Domain("test_images", "Images", "", "test_images", "test_images", "", "")
                    fake = Mock(dim=2)
                    fake.embed_passages.side_effect = lambda parts: np.tile([1., 0.], (len(parts), 1))
                    with patch.object(extract, "ocr_image", return_value="Returns accepted within thirty days."), patch.object(embed, "get_embedder", return_value=fake):
                        result = ingest.ingest_pdf(domain, image, use_llm=False)
                        self.assertEqual(result["pages"], 1)
                        self.assertEqual(result["name"], "returns.png")
                        self.assertEqual(result["extracted_text"], "Returns accepted within thirty days.")
                        hits = vectorstore.search(domain.collection, np.array([1., 0.]), 5)
                        self.assertTrue(any("thirty days" in hit["text"] for hit in hits))
                        self.assertEqual(ingest.ingest_pdf(domain, image, use_llm=False)["status"], "skipped")
                finally:
                    vectorstore.close_client()

    def test_preview_endpoint_returns_raw_text_and_checks_domain_membership(self):
        from auth_helpers import authenticated_api
        import api_server
        from fastapi.testclient import TestClient
        from rag import ingest
        digest = "a" * 64
        text = "<script>original OCR text</script>\nThirty days."
        headers = {"Cookie": "qa_access=test-access"}
        with authenticated_api(), patch.object(config, "CLOUD_MODE", False), patch.object(api_server, "_domain"), patch.object(ingest, "list_ingested", return_value={digest: {"name": "notes.png", "extracted_text": text}}):
            with TestClient(api_server.app) as client:
                path = f"/api/domains/example/documents/{digest}/text"
                self.assertEqual(client.get(path).status_code, 401)
                response = client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["text"], text)
                self.assertEqual(client.get(path.replace(digest, "b" * 64), headers=headers).status_code, 404)

    @unittest.skipUnless(config.tesseract_cmd(), "Tesseract is not installed")
    def test_real_image_and_scanned_pdf_text(self):
        # Missing-engine test temporarily modifies pytesseract's module setting.
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(config, DATA_DIR=root, CACHE_DIR=root / "cache"):
                image = root / "returns.png"
                image.write_bytes(image_bytes())
                pdf = root / "returns.pdf"
                pdf.write_bytes(extract.document_pdf(image.name, image.read_bytes()))
                from rag import cloud
                with patch.dict("os.environ", {"OCR_PROVIDER": "tesseract"}):
                    with pymupdf.open(pdf) as doc:
                        self.assertIn("thirty days", cloud._ocr_page(doc, 0).lower())
                for document in (image, pdf):
                    pages = extract.extract_pdf(document, use_cache=False)
                    text = " ".join(pages[0]["image_text"]).lower()
                    self.assertIn("thirty days", text)
                    self.assertTrue(pages[0]["is_image"])


if __name__ == "__main__":
    unittest.main()
