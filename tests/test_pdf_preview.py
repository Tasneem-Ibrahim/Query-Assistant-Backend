"""PDF previews require authentication and domain membership."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import api_server
from fastapi.testclient import TestClient
from rag import cloud, config, extract, ingest


class PdfPreviewTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.multiple(config, CLOUD_MODE=False, ADMIN_USERNAME="Admin", ADMIN_PASSWORD="test-password")
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.headers = {"Authorization": "Basic " + base64.b64encode(b"Admin:test-password").decode()}

    def test_local_preview_and_access_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.pdf"
            content = b"%PDF-1.4 test document"
            path.write_bytes(content)
            digest = extract.file_sha256(path)
            domain = Mock()
            domain.default_pdfs.return_value = [path]
            with patch.object(api_server, "_domain", return_value=domain), patch.object(ingest, "list_ingested", return_value={digest: {"name": path.name}}), TestClient(api_server.app) as client:
                url = f"/api/domains/example/documents/{digest}/pdf"
                self.assertEqual(client.get(url).status_code, 401)
                response = client.get(url, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, content)
                self.assertEqual(response.headers["content-type"], "application/pdf")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(client.get(url.replace(digest, "b" * 64), headers=self.headers).status_code, 404)
                self.assertEqual(client.get(url.replace(digest, "invalid"), headers=self.headers).status_code, 400)
                path.write_bytes(b"replaced file")
                self.assertEqual(client.get(url, headers=self.headers).status_code, 404)

    def test_cloud_preview_reads_only_registered_object(self):
        digest = "a" * 64
        row = {"file_hash": digest, "name": "notes.pdf", "object_path": "private/example/notes.pdf"}
        with patch.object(config, "CLOUD_MODE", True), patch.object(api_server, "_domain"), patch.object(cloud, "documents", return_value=[row]), patch.object(cloud, "read_document", return_value=b"%PDF-cloud") as read, TestClient(api_server.app) as client:
            url = f"/api/domains/example/documents/{digest}/pdf"
            self.assertEqual(client.get(url, headers=self.headers).content, b"%PDF-cloud")
            read.assert_called_once_with(row["object_path"])
            row["name"] = "image.png"
            self.assertEqual(client.get(url, headers=self.headers).status_code, 400)
            self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
