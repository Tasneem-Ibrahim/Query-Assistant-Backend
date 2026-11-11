"""Confidence selection and real OCR regression coverage."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rag import config, extract


class AdaptiveOCRTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new('RGB', (300, 100), 'white')

    def test_clear_confident_baseline_avoids_retries(self):
        with patch.object(extract, '_read_ocr', return_value=('Clear accurate text here', 96, 96, 24)) as read, patch.object(extract, '_ocr_variants') as variants:
            self.assertEqual(extract.ocr_image(self.image, psm=3), 'Clear accurate text here')
            read.assert_called_once()
            self.assertEqual(read.call_args.args[1], 3)
            variants.assert_not_called()

    def test_explicit_layout_still_gets_enhancement_and_best_confidence(self):
        with patch.object(extract, '_ocr_variants', return_value=iter([self.image, self.image])), patch.object(extract, '_read_ocr', side_effect=[('bad', 25, 3, 3), ('long garbage text', 30, 5, 30), ('Recovered accurate text here', 96, 96, 25)]) as read:
            self.assertEqual(extract.ocr_image(self.image, psm=3), 'Recovered accurate text here')
            self.assertEqual(read.call_count, 3)

    def test_lower_quality_and_timeout_retries_preserve_baseline(self):
        with patch.object(extract, '_ocr_variants', return_value=iter([self.image, self.image])), patch.object(extract, '_read_ocr', side_effect=[('baseline', 70, 60, 8), RuntimeError('timeout'), ('garbage much longer', 50, 20, 30), ('short', 40, 10, 5), RuntimeError('timeout'), ('worse', 30, 5, 5)]) as read:
            self.assertEqual(extract.ocr_image(self.image, psm=3), 'baseline')
            self.assertEqual(read.call_count, 6)

    def test_confident_partial_retry_cannot_drop_footer(self):
        full = ('Heading and all policy lines including quiet environment', 90, 87, 168)
        partial = ('Heading and policy without footer', 92, 90, 122)
        self.assertFalse(extract._prefer_ocr(partial, full))
        self.assertTrue(extract._prefer_ocr(('Corrected full policy', 96, 96, 168), full))

    @unittest.skipUnless(config.tesseract_cmd(), 'Tesseract is not installed')
    def test_hospital_notice_keeps_last_line(self):
        path = Path(__file__).resolve().parents[2] / 'documents' / 'Hospital Visiting Policy Notice.png'
        if not path.exists():
            self.skipTest('Local hospital notice fixture is unavailable')
        import io
        import pymupdf
        with Image.open(path) as image:
            text = extract.ocr_image(image, psm=3).lower()
        self.assertIn('please maintain a', text)
        self.assertIn('environment', text)
        # Cloud OCR renders the normalized PDF at 200 DPI first.
        with pymupdf.open(stream=extract.document_pdf(path.name, path.read_bytes()), filetype='pdf') as doc:
            pix = doc[0].get_pixmap(dpi=200)
            with Image.open(io.BytesIO(pix.tobytes('png'))) as image:
                text = extract.ocr_image(image, psm=3).lower()
        self.assertIn('please maintain a', text)
        self.assertIn('environment', text)

    def test_blank_image_returns_empty(self):
        with patch.object(extract, '_read_ocr', return_value=('', 0, 0, 0)) as read:
            self.assertEqual(extract.ocr_image(self.image, psm=12), '')
            self.assertEqual(read.call_count, 3)

    def test_word_confidence_ignores_structure_and_preserves_lines(self):
        data = dict(text=['', 'Hello', 'world', 'Next'], conf=[-1, 90, 70, 80],
                    page_num=[1]*4, block_num=[1]*4, par_num=[1]*4, line_num=[0, 1, 1, 2])
        with patch('pytesseract.image_to_data', return_value=data):
            text, confidence, quality, characters = extract._read_ocr(self.image, 3)
        self.assertEqual(text, 'Hello world\nNext')
        self.assertEqual(confidence, 80)
        self.assertEqual(characters, 14)
        self.assertEqual(quality, 56)

    def test_old_cache_is_not_reused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'test.png'
            self.image.save(path)
            digest = extract.file_sha256(path)
            (root / f'{digest}.ocr-v2.raw.json').write_text('[]')
            with patch.multiple(config, DATA_DIR=root, CACHE_DIR=root), patch.object(extract, 'ocr_image', return_value='New extraction') as read:
                self.assertEqual(extract.extract_pdf(path)[0]['image_text'], ['New extraction'])
                extract.extract_pdf(path)
                read.assert_called_once()
            self.assertTrue((root / f'{digest}.ocr-v5.raw.json').exists())

    def test_alpha_and_working_size_bound(self):
        result = extract._prepare_for_ocr(Image.new('RGBA', (20, 20000), (0, 0, 0, 0)))
        self.assertEqual(result.getextrema(), (255, 255))
        self.assertLessEqual(result.width * result.height, 12_010_000)

    def test_deskew_improves_line_alignment(self):
        image = Image.new('L', (1000, 400), 255)
        draw = ImageDraw.Draw(image)
        for y in (60, 140, 220):
            draw.text((40, y), 'Return receipt delivery support', font=ImageFont.load_default(size=38), fill=0)
        skewed = image.rotate(6, expand=True, fillcolor=255)
        corrected = extract._deskew(skewed)
        def alignment(img):
            rows = (np.asarray(img) < 180).sum(axis=1).astype(float)
            return np.square(np.diff(rows)).sum()
        self.assertGreater(alignment(corrected), alignment(skewed) * 1.5)

    @unittest.skipUnless(config.tesseract_cmd(), 'Tesseract is not installed')
    def test_real_comparison_clear_and_degraded(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from compare_ocr import compare
        results = compare()['results']
        for row in results:
            with self.subTest(sample=row['sample']):
                self.assertGreaterEqual(row['after_similarity'], row['before_similarity'])
        self.assertTrue(any(row['after_similarity'] > row['before_similarity'] for row in results))


if __name__ == '__main__':
    unittest.main()
