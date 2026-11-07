"""Custom knowledge base persistence tests; no models, API keys, or pip packages needed."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor


class CustomDomainsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = types.ModuleType("custom_domain_test_rag")
        self.package.__path__ = []
        self.package.config = types.SimpleNamespace(DATA_DIR=self.root / "data", PDF_DIR=self.root / "pdfs")
        sys.modules[self.package.__name__] = self.package
        self.module = self.load_module()

    def load_module(self):
        name = self.package.__name__ + ".domains"
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "src/rag/domains.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def tearDown(self):
        sys.modules.pop(self.package.__name__ + ".domains", None)
        sys.modules.pop(self.package.__name__, None)
        self.temp.cleanup()

    def test_create_persists_across_restart_and_isolates_paths(self):
        biology = self.module.create_domain("  Biology Notes  ")
        research = self.module.create_domain("My Research")
        self.assertEqual(biology.label, "Biology Notes")
        self.assertNotEqual(biology.collection, research.collection)
        self.assertNotEqual(biology.pdf_dir, research.pdf_dir)
        reloaded = self.load_module()
        self.assertEqual(reloaded.get_domain(biology.key).label, "Biology Notes")
        self.assertEqual(len(reloaded.list_domains()), 4)
        self.assertIn("any topic", reloaded.get_domain(biology.key).persona)

    def test_rename_keeps_collection_and_existing_pdf(self):
        original = self.module.create_domain("Science")
        original.pdf_dir.mkdir(parents=True)
        pdf = original.pdf_dir / "notes.pdf"
        pdf.write_bytes(b"test fixture")
        renamed = self.module.rename_domain(original.key, "Biology")
        self.assertEqual(renamed.key, original.key)
        self.assertEqual(renamed.collection, original.collection)
        self.assertEqual(renamed.default_pdfs(), [pdf])
        self.assertEqual(self.load_module().get_domain(original.key).label, "Biology")

    def test_names_are_validated_and_duplicates_rejected(self):
        for name in ["", "  ", "a" * 81, "bad\nname", "customer support"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.module.create_domain(name)
        original = self.module.create_domain("Biology")
        with self.assertRaises(ValueError):
            self.module.create_domain(" biology ")
        self.assertEqual(self.module.rename_domain(original.key, "BIOLOGY").label, "BIOLOGY")
        with self.assertRaises(ValueError):
            self.module.rename_domain("customer_support", "Changed")

    def test_unicode_and_path_like_labels_cannot_control_storage_path(self):
        domain = self.module.create_domain("../میری کتاب")
        self.assertEqual(domain.pdf_dir.parent, self.package.config.PDF_DIR)
        self.assertEqual(domain.pdf_dir.name, domain.key)
        self.assertEqual(self.load_module().get_domain(domain.key).label, "../میری کتاب")

    def test_parallel_creates_do_not_lose_saved_names(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            created = list(pool.map(self.module.create_domain, [f"Topic {i}" for i in range(12)]))
        self.assertEqual(len({d.key for d in created}), 12)
        self.assertEqual(len(self.load_module().list_domains()), 14)

    def test_corrupt_registry_is_not_overwritten(self):
        path = self.root / "data/custom_domains.json"
        path.parent.mkdir()
        path.write_text('{"../../escape": "Invalid"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.module.create_domain("New")
        self.assertEqual(json.loads(path.read_text()), {"../../escape": "Invalid"})

    def test_delete_removes_only_selected_base_and_its_pdfs(self):
        selected = self.module.create_domain("Atomic Habit")
        other = self.module.create_domain("Other topic")
        for domain in [selected, other]:
            domain.pdf_dir.mkdir(parents=True)
            (domain.pdf_dir / "book.pdf").write_bytes(b"fixture")
        reset = Mock()
        self.package.ingest = types.SimpleNamespace(reset_domain=reset)
        self.module.delete_domain(selected.key)
        reset.assert_called_once_with(selected.key)
        self.assertFalse(selected.pdf_dir.exists())
        self.assertTrue((other.pdf_dir / "book.pdf").exists())
        self.assertNotIn(selected.key, [d.key for d in self.module.list_domains()])
        with self.assertRaises(ValueError):
            self.load_module().get_domain(selected.key)
        self.assertEqual(self.module.get_domain(other.key).label, "Other topic")

    def test_delete_rejects_builtins_and_unknown_keys_without_cleanup(self):
        reset = Mock()
        self.package.ingest = types.SimpleNamespace(reset_domain=reset)
        for key in ["customer_support", "legal_assistant", "../../outside"]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.module.delete_domain(key)
        reset.assert_not_called()

    def test_delete_vector_failure_keeps_pdfs_and_name_for_retry(self):
        domain = self.module.create_domain("Atomic Habit")
        domain.pdf_dir.mkdir(parents=True)
        pdf = domain.pdf_dir / "book.pdf"
        pdf.write_bytes(b"fixture")
        self.package.ingest = types.SimpleNamespace(reset_domain=Mock(side_effect=RuntimeError("offline")))
        with self.assertRaises(RuntimeError):
            self.module.delete_domain(domain.key)
        self.assertTrue(pdf.exists())
        self.assertEqual(self.load_module().get_domain(domain.key).label, "Atomic Habit")


if __name__ == "__main__":
    unittest.main()
