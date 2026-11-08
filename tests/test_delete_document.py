"""Exercise per-document deletion against temporary files and a real local Qdrant."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rag import config, extract, ingest, vectorstore
from rag.domains import get_domain


class DeleteDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        vectorstore.close_client()
        self.settings = patch.multiple(config, DATA_DIR=root / "data", PDF_DIR=root / "pdfs",
            CACHE_DIR=root / "cache", REGISTRY_FILE=root / "registry.json",
            QDRANT_PATH=root / "vectors", QDRANT_URL="")
        self.settings.start()
        self.domain = get_domain("customer_support")
        self.domain.pdf_dir.mkdir(parents=True)
        self.first = self.domain.pdf_dir / "atomic.pdf"
        self.second = self.domain.pdf_dir / "support.pdf"
        self.first.write_bytes(b"Atomic habits fixture")
        self.second.write_bytes(b"Customer support fixture")
        self.hashes = [extract.file_sha256(p) for p in [self.first, self.second]]
        self.registry = {self.domain.key: {
            digest: {"name": pdf.name, "chunks": 1, "pages": 1}
            for pdf, digest in zip([self.first, self.second], self.hashes)
        }, "legal_assistant": {"other": {"name": "contract.pdf"}}}
        ingest._save_registry(self.registry)
        vectorstore.ensure_collection(self.domain.collection, 2)
        vectorstore.upsert(self.domain.collection, [
            {"id": i + 1, "text": pdf.name, "metadata": {"file_hash": digest}}
            for i, (pdf, digest) in enumerate(zip([self.first, self.second], self.hashes))
        ], np.array([[1., 0.], [0., 1.]], dtype="float32"))

    def tearDown(self):
        vectorstore.close_client()
        self.settings.stop()
        self.temp.cleanup()

    def test_delete_preserves_other_document_vectors_and_registry(self):
        ingest.delete_document(self.domain.key, self.hashes[0])
        self.assertFalse(self.first.exists())
        self.assertTrue(self.second.exists())
        self.assertEqual(vectorstore.count(self.domain.collection), 1)
        hits = vectorstore.search(self.domain.collection, np.array([1., 0.]), 10)
        self.assertEqual([h["metadata"]["file_hash"] for h in hits], [self.hashes[1]])
        saved = ingest._load_registry()
        self.assertNotIn(self.hashes[0], saved[self.domain.key])
        self.assertIn(self.hashes[1], saved[self.domain.key])
        self.assertEqual(saved["legal_assistant"], self.registry["legal_assistant"])

    def test_unknown_document_does_not_change_files_or_vectors(self):
        with self.assertRaises(KeyError):
            ingest.delete_document("legal_assistant", self.hashes[0])
        self.assertTrue(self.first.exists())
        self.assertEqual(vectorstore.count(self.domain.collection), 2)

    def test_overwritten_pdf_is_not_deleted_for_an_older_hash(self):
        self.first.write_bytes(b"replacement PDF")
        ingest.delete_document(self.domain.key, self.hashes[0])
        self.assertEqual(self.first.read_bytes(), b"replacement PDF")
        self.assertEqual(vectorstore.count(self.domain.collection), 1)

    def test_vector_failure_keeps_pdf_and_registry(self):
        with patch.object(vectorstore, "delete_file", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                ingest.delete_document(self.domain.key, self.hashes[0])
        self.assertTrue(self.first.exists())
        self.assertIn(self.hashes[0], ingest._load_registry()[self.domain.key])


class CloudDeleteTests(unittest.TestCase):
    def test_legacy_collection_is_indexed_before_filtered_deletion(self):
        client = Mock()
        schema = {}
        client.get_collection.return_value.payload_schema = schema

        def create_index(**kwargs):
            self.assertEqual(kwargs["field_name"], "file_hash")
            self.assertEqual(kwargs["field_schema"], vectorstore.models.PayloadSchemaType.KEYWORD)
            self.assertTrue(kwargs["wait"])
            schema["file_hash"] = Mock()

        def delete(**kwargs):
            if "file_hash" not in schema:
                raise RuntimeError("Index required but not found for file_hash")
            condition = kwargs["points_selector"].filter.must[0]
            self.assertEqual(condition.key, "file_hash")
            self.assertEqual(condition.match.value, "target-hash")

        client.create_payload_index.side_effect = create_index
        client.delete.side_effect = delete
        with patch.object(config, "QDRANT_URL", "https://qdrant.example"), patch.object(vectorstore, "get_client", return_value=client):
            vectorstore.delete_file("customer_support", "target-hash")
            vectorstore.delete_file("customer_support", "target-hash")
        client.create_payload_index.assert_called_once()
        self.assertEqual(client.delete.call_count, 2)

    def test_new_cloud_collection_gets_file_hash_index(self):
        client = Mock()
        client.collection_exists.return_value = False
        client.get_collection.return_value.payload_schema = {}
        with patch.object(config, "QDRANT_URL", "https://qdrant.example"), patch.object(vectorstore, "get_client", return_value=client):
            vectorstore.ensure_collection("customer_support", 2)
        client.create_collection.assert_called_once()
        client.create_payload_index.assert_called_once()

    def test_index_failure_prevents_deletion(self):
        client = Mock()
        client.get_collection.return_value.payload_schema = {}
        client.create_payload_index.side_effect = RuntimeError("Index creation failed")
        with patch.object(config, "QDRANT_URL", "https://qdrant.example"), patch.object(vectorstore, "get_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "Index creation failed"):
                vectorstore.delete_file("customer_support", "target-hash")
        client.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
