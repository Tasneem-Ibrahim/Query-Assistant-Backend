"""Offline cloud integration: real PDFs/Qdrant, fake Supabase and embeddings API."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rag import clean, cloud, config, embed, vectorstore
from rag.domains import Domain


class CloudPipelineTests(unittest.TestCase):
    def setUp(self):
        provider = patch.dict("os.environ", {"OCR_PROVIDER": "api"})
        provider.start()
        self.addCleanup(provider.stop)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        vectorstore.close_client()
        self.settings = patch.multiple(config, CLOUD_MODE=True, QDRANT_URL="",
            QDRANT_PATH=root / "qdrant", DATA_DIR=root, CACHE_DIR=root / "cache",
            EMBEDDING_API_URL="https://embed.example/embeddings", EMBEDDING_API_MODEL="model-a",
            EMBEDDING_DIMENSIONS=2)
        self.settings.start()
        self.domain = Domain("customer_support", "Support", "", config.embedding_namespace() + "_customer_support", "customer_support", "", "")
        self.row = None
        self.objects = {}
        self.requests = patch.object(cloud, "_request", side_effect=self.request)
        self.requests.start()
        self.docs = patch.object(cloud, "documents", side_effect=lambda _: [copy.deepcopy(self.row)] if self.row else [])
        self.docs.start()
        self.errors = patch.object(config, "configuration_errors", return_value=[])
        self.errors.start()
        self.fake_embedder = Mock(dim=2)
        self.fake_embedder.embed_passages.side_effect = lambda chunks: np.tile([1., 0.], (len(chunks), 1))
        self.embed_patch = patch.object(embed, "get_embedder", return_value=self.fake_embedder)
        self.embed_patch.start()

    def tearDown(self):
        self.embed_patch.stop()
        self.errors.stop()
        self.docs.stop()
        self.requests.stop()
        vectorstore.close_client()
        self.settings.stop()
        self.temp.cleanup()

    def request(self, method, path, **kwargs):
        data = kwargs.get("json", {})
        if path.startswith("/storage/v1/object/"):
            if method == "POST":
                # Match the production bucket's PDF-only MIME restriction.
                self.assertEqual(kwargs["headers"]["Content-Type"], "application/pdf")
                with pymupdf.open(stream=kwargs["content"], filetype="pdf") as pdf:
                    self.assertGreaterEqual(len(pdf), 1)
                self.objects[path] = kwargs["content"]
            if method == "GET":
                return httpx.Response(200, content=self.objects[path])
            return httpx.Response(200, json={})
        if path == "/rest/v1/qa_documents":
            if method == "POST":
                self.row = {**data, "next_page": 0, "chunks": 0, "image_pages": 0, "status": "pending", "cached_chunks": None}
            if method == "PATCH":
                self.row.update(data)
                return httpx.Response(200, json=[self.row])
            if method == "DELETE":
                self.row = None
            return httpx.Response(200, json={})
        if path.endswith("qa_claim_document"):
            return httpx.Response(200, json=self.row)
        if path.endswith("qa_release_document"):
            return httpx.Response(200, json=None)
        if path.endswith("qa_advance_document"):
            self.row["next_page"] += 1
            self.row["chunks"] += data["p_chunks"]
            self.row["image_pages"] += data["p_image"]
            self.row["cached_chunks"] = None
            if self.row["next_page"] == self.row["pages"]:
                self.row["status"] = "ready"
            return httpx.Response(200, json=self.row)
        raise AssertionError((method, path))

    def upload(self):
        with pymupdf.open() as pdf:
            for text in ["Customer support accepts returns within thirty days.", "Delivery support is available every Monday."]:
                pdf.new_page().insert_text((40, 40), text)
            content = pdf.tobytes()
        return cloud.start(self.domain, "support.pdf", content, use_llm=False)["file_hash"]

    def test_upload_resume_and_publish_only_when_all_pages_complete(self):
        digest = self.upload()
        result = cloud.step(self.domain, digest)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(vectorstore.count(self.domain.collection), 1)
        self.assertEqual(vectorstore.search(self.domain.collection, np.array([1., 0.]), 10), [])
        self.assertEqual(cloud.list_ingested(self.domain.key), {})
        result = cloud.step(self.domain, digest)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["report"]["chunks"], 2)
        self.assertEqual(len(vectorstore.search(self.domain.collection, np.array([1., 0.]), 10)), 2)
        cloud.step(self.domain, digest)  # Completed jobs do not re-embed or duplicate counts.
        self.assertEqual(self.fake_embedder.embed_passages.call_count, 2)

    def test_image_upload_indexes_original_filename(self):
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (600, 400), "white").save(buffer, format="PNG")
        digest = cloud.start(self.domain, "returns.png", buffer.getvalue(), use_llm=False)["file_hash"]
        with patch.object(cloud, "_ocr_page", return_value="Returns are accepted within thirty days."):
            result = cloud.step(self.domain, digest)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["report"]["pages"], 1)
        self.assertEqual(result["report"]["name"], "returns.png")
        self.assertEqual(cloud.read_image_text(self.row["object_path"]), "Returns are accepted within thirty days.")
        hits = vectorstore.search(self.domain.collection, np.array([1., 0.]), 10)
        self.assertTrue(any("thirty days" in hit["text"] for hit in hits))

    def test_preview_preserves_unicode_and_lines_in_pdf_only_bucket(self):
        text = "English and اردو\n<original OCR text>\nSecond line."
        path = config.embedding_namespace() + "/customer_support/sample.pdf"
        cloud._save_image_text(path, text)
        self.assertEqual(cloud.read_image_text(path), text)
        self.assertIn(cloud._object_path(path + ".ocr.pdf"), self.objects)

    def test_multipage_preview_retry_keeps_each_page_once(self):
        path = config.embedding_namespace() + "/customer_support/scan.pdf"
        cloud._save_image_text(path, "First page", page=1)
        cloud._save_image_text(path, "Second page", page=2)
        cloud._save_image_text(path, "Second page", page=2)
        text = cloud.read_image_text(path)
        self.assertEqual(text, "Page 1\nFirst page\n\nPage 2\nSecond page")

    def test_multipage_tiff_cloud_processing_keeps_both_pages(self):
        import io
        from PIL import Image
        buffer = io.BytesIO()
        first = Image.new("RGB", (400, 200), "white")
        first.save(buffer, format="TIFF", save_all=True, append_images=[first.copy()])
        digest = cloud.start(self.domain, "scan.tiff", buffer.getvalue(), use_llm=False)["file_hash"]
        with patch.object(cloud, "_ocr_page", side_effect=["First page accepts returns.", "Second page covers delivery."]):
            self.assertEqual(cloud.step(self.domain, digest)["status"], "pending")
            result = cloud.step(self.domain, digest)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["report"]["pages"], 2)
        text = cloud.read_image_text(self.row["object_path"])
        self.assertIn("First page accepts returns.", text)
        self.assertIn("Second page covers delivery.", text)

    def test_retry_after_partial_upsert_keeps_cached_chunks_and_counts(self):
        digest = self.upload()
        original = vectorstore.upsert

        def failed_upsert(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Network disconnected after upsert")

        with patch.object(vectorstore, "upsert", side_effect=failed_upsert):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                cloud.step(self.domain, digest)
        self.assertEqual(self.row["next_page"], 0)
        self.assertIsNotNone(self.row["cached_chunks"])
        with patch.object(clean, "clean_pages", side_effect=AssertionError("Cached page must not be cleaned again")):
            cloud.step(self.domain, digest)
        self.assertEqual(self.row["chunks"], 1)
        self.assertEqual(vectorstore.count(self.domain.collection), 1)

    def test_vector_delete_failure_preserves_storage_and_metadata(self):
        digest = self.upload()
        with patch.object(vectorstore, "delete_file", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                cloud.delete_document(self.domain, digest)
        self.assertIsNotNone(self.row)
        self.assertEqual(len(self.objects), 1)

    def test_namespace_changes_with_model_even_at_equal_dimension(self):
        original = config.embedding_namespace()
        with patch.object(config, "EMBEDDING_API_MODEL", "model-b"):
            self.assertNotEqual(original, config.embedding_namespace())

    def test_scanned_page_without_ocr_key_fails_explicitly(self):
        with pymupdf.open() as pdf, patch.object(config, "OCR_API_KEY", ""):
            pdf.new_page()
            with self.assertRaisesRegex(ValueError, "OCR_API_KEY"):
                cloud._ocr_page(pdf, 0)

    def test_ocr_sends_only_the_requested_page(self):
        import base64

        def post(url, **kwargs):
            document = kwargs["json"]["document"]["document_url"]
            with pymupdf.open(stream=base64.b64decode(document.split(",", 1)[1]), filetype="pdf") as single:
                self.assertEqual(len(single), 1)
                self.assertIn("second", single[0].get_text())
            return httpx.Response(200, json={"pages": [{"markdown": "Recognized table"}]})

        with pymupdf.open() as pdf, patch.object(config, "OCR_API_KEY", "test"), patch.object(httpx, "post", side_effect=post):
            pdf.new_page().insert_text((40, 40), "first")
            pdf.new_page().insert_text((40, 40), "second")
            self.assertEqual(cloud._ocr_page(pdf, 1), "Recognized table")

    def test_invalid_pdf_is_rejected_without_saving(self):
        with self.assertRaisesRegex(ValueError, "readable PDF"):
            cloud.start(self.domain, "bad.pdf", b"not a PDF")
        self.assertEqual(self.objects, {})


class APIEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.multiple(config, EMBEDDING_API_URL="https://embed.example/embeddings",
            EMBEDDING_API_KEY="fake-test-key", EMBEDDING_API_MODEL="model-a", EMBEDDING_DIMENSIONS=2)
        self.settings.start()

    def tearDown(self):
        self.settings.stop()

    def test_order_and_normalization(self):
        response = httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 0]}]})
        with patch.object(httpx, "post", return_value=response):
            np.testing.assert_allclose(embed.APIEmbedder().embed_passages(["a", "b"]), [[1, 0], [0, 1]])

    def test_rejects_invalid_provider_vectors(self):
        for data in [[{"index": 0, "embedding": [1, 2, 3]}], [{"index": 0, "embedding": [0, 0]}], []]:
            with self.subTest(data=data), patch.object(httpx, "post", return_value=httpx.Response(200, json={"data": data})):
                with self.assertRaises(ValueError):
                    embed.APIEmbedder().embed_query("a")

    def test_provider_errors_do_not_expose_response_secrets(self):
        with patch.object(httpx, "post", return_value=httpx.Response(401, text="fake-test-key")):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as result:
                embed.APIEmbedder().embed_query("a")
            self.assertNotIn("fake-test-key", str(result.exception))


class CloudAPITests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import api_server

        self.server = api_server
        from auth_helpers import authenticated_api
        self.auth = authenticated_api()
        self.auth.__enter__()
        self.addCleanup(self.auth.__exit__, None, None, None)
        self.client = TestClient(api_server.app, headers={"Cookie": "qa_access=test-access", "X-CSRF-Protection": "1"})
        self.cloud_mode = patch.object(config, "CLOUD_MODE", True)
        self.cloud_mode.start()
        self.domain_patch = patch.object(api_server, "get_domain", return_value=Mock(key="customer_support"))
        self.domain_patch.start()

    def tearDown(self):
        self.domain_patch.stop()
        self.cloud_mode.stop()
        self.client.close()

    def test_cloud_upload_starts_a_job_without_a_background_thread(self):
        with patch.object(cloud, "start", return_value={"file_hash": "a" * 64, "status": "pending"}), patch.object(self.server.threading, "Thread", side_effect=AssertionError("No background threads in cloud mode")):
            response = self.client.post("/api/domains/customer_support/ingest", files={"file": ("guide.pdf", b"fixture", "application/pdf")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["jobs"][0]["status"], "pending")

    def test_image_upload_uses_the_same_endpoint(self):
        with patch.object(cloud, "start", return_value={"file_hash": "b" * 64, "status": "pending"}) as start:
            response = self.client.post("/api/domains/customer_support/ingest", files={"file": ("notes.PNG", b"fixture", "image/png")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[1], "notes.PNG")

    def test_oversized_upload_is_rejected_before_storage(self):
        with patch.object(cloud, "start") as start:
            response = self.client.post("/api/domains/customer_support/ingest", files={"file": ("big.pdf", b"x" * (config.MAX_UPLOAD_BYTES + 1), "application/pdf")})
        self.assertEqual(response.status_code, 413)
        start.assert_not_called()

    def test_cloud_reset_is_explicitly_disabled(self):
        response = self.client.post("/api/domains/customer_support/reset")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
