"""Safety contracts for spatial bullets and consensus-based local OCR retries."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rag import config, extract, ocr_refine as refine
from rag.ocr_refine import Word


def word(text, confidence=70, left=50, top=30, width=80, height=25):
    return Word(text, confidence, left, top, width, height)


def result(image, lines):
    data = {key: [] for key in ('text', 'conf', 'left', 'top', 'width', 'height', 'page_num', 'block_num', 'par_num', 'line_num')}
    for number, words in enumerate(lines, 1):
        for w in words:
            for field in ('text', 'left', 'top', 'width', 'height'):
                data[field].append(getattr(w, field))
            data['conf'].append(w.confidence)
            for field in ('page_num', 'block_num', 'par_num'):
                data[field].append(1)
            data['line_num'].append(number)
    return extract._OCRResult(('\n'.join(' '.join(w.text for w in words) for words in lines), 80, 80, 100), data, image)


class TargetedOCRTests(unittest.TestCase):
    def test_two_agreeing_visual_reads_can_fix_uncertain_character(self):
        self.assertEqual(refine._replacement(word('quict', 70), word('quiet', 95), word('quiet', 94)), 'quiet')
        self.assertEqual(refine._replacement(word('\ufffd', 63), word('a', 96), word('a', 95)), 'a')

    def test_preserve_confident_tokens_numbers_case_and_punctuation(self):
        for source, proposed in [('quiet', 'quict'), ('10:00', '18:00'), ('8:00', '6:00'),
                                 ('2', '3'), ('AM', 'PM'), ('mask.', 'mask'), ('Name', 'name'),
                                 ('patient', '1234567'), ('@', ''), ('\u00ab', 'a'), ('10/11/2026', '11/11/2026')]:
            confidence = 96 if source in {'quiet', 'AM'} else 60
            with self.subTest(source=source):
                self.assertEqual(refine._replacement(word(source, confidence), word(proposed, 99), word(proposed, 99)), source)

    def test_disagreement_lower_confidence_and_unrelated_words_are_unchanged(self):
        original = word('quict', 75)
        for a, b in [(word('quiet', 99), word('quick', 99)), (word('quiet', 78), word('quiet', 78)),
                     (word('other', 99), word('other', 99))]:
            self.assertEqual(refine._replacement(original, a, b), original.text)

    def test_full_line_retry_preserves_confident_neighbours_and_line_order(self):
        image = Image.new('L', (600, 150), 255)
        original = result(image, [[word('wear', 96, 50), word('\ufffd', 63, 150, width=20), word('mask.', 96, 200)],
                                  [word('10:00', 95, 50, top=90), word('AM', 96, 150, top=90)]])
        # Crop origin is 42, white padding is 10.
        reads = [word('wear', 96, 18), word('a', 96, 118, width=20), word('mask.', 96, 168)]
        with patch.object(refine, '_read_line', return_value=reads) as read:
            self.assertEqual(refine.refine_ocr(original), 'wear a mask.\n10:00 AM')
            self.assertEqual(read.call_count, 2)

    def test_changed_token_count_spatial_mismatch_and_timeout_preserve_text(self):
        image = Image.new('L', (600, 150), 255)
        original = result(image, [[word('quict', 70)]])
        for answers in ([[] , []], [[word('quiet', 99, 500)], [word('quiet', 99, 500)]]):
            with patch.object(refine, '_read_line', side_effect=answers):
                self.assertEqual(refine.refine_ocr(original), 'quict')
        with patch.object(refine, '_read_line', side_effect=RuntimeError('timeout')):
            self.assertEqual(refine.refine_ocr(original), 'quict')

    def test_disagreeing_protected_context_rejects_entire_line_retry(self):
        image = Image.new('L', (600, 150), 255)
        original = result(image, [[word('wear', 96, 50), word('\ufffd', 63, 150, width=20)]])
        reads = [word('were', 99, 18), word('a', 99, 118, width=20)]
        with patch.object(refine, '_read_line', return_value=reads):
            self.assertEqual(refine.refine_ocr(original), original[0])

    def test_retry_budget_is_four_lines(self):
        image = Image.new('L', (600, 1000), 255)
        original = result(image, [[word('uncertain', 50, top=i*70+20)] for i in range(10)])
        with patch.object(refine, '_read_line', return_value=[]) as read:
            self.assertEqual(refine.refine_ocr(original), original[0])
            self.assertEqual(read.call_count, 8)

    def test_round_aligned_bullets_normalize_but_real_at_symbols_remain(self):
        image = Image.new('L', (700, 300), 255)
        draw = ImageDraw.Draw(image)
        lines = []
        for top in (40, 120, 200):
            draw.ellipse((30, top, 49, top+19), fill=0)
            lines.append([word('@', 75, 30, top, 20, 20), word('Keep', 96, 90, top-5, 80, 35), word('quiet.', 96, 200, top-5, 100, 35)])
        original = result(image, lines)
        text = refine.refine_ocr(original)
        self.assertEqual(text, '\n'.join(['\u2022 Keep quiet.']*3))
        # A true @ glyph has internal whitespace and must not be treated as a disk.
        glyph = Image.new('L', (100, 100), 255)
        ImageDraw.Draw(glyph).text((10,10), '@', font=ImageFont.load_default(size=45), fill=0)
        box = ImageOps_invert_bbox(glyph)
        left, top, right, bottom = box
        self.assertFalse(refine._filled_bullet(glyph, word('@', 75, left, top, right-left, bottom-top)))

    def test_isolated_disk_and_inline_symbols_remain(self):
        image = Image.new('L', (500, 100), 255)
        ImageDraw.Draw(image).ellipse((30,40,49,59), fill=0)
        original = result(image, [[word('@', 96, 30,40,20,20), word('example.com',96,90,35,200,35), word('email',96,310,35,80,35)]])
        self.assertEqual(refine.refine_ocr(original), original[0])

    @unittest.skipUnless(config.tesseract_cmd(), 'Tesseract is not installed')
    def test_real_clear_and_blurred_protected_content(self):
        image = Image.new('RGB', (1500, 400), 'white')
        text = 'Visiting Hours: 10:00 AM to 8:00 PM.\nMaximum of 2 visitors per patient.\nEmail: care@example.com; Fee: $25.50.'
        ImageDraw.Draw(image).multiline_text((50,60), text, font=ImageFont.load_default(size=40), fill='black', spacing=25)
        for name, sample in [('clear', image), ('blurred', image.filter(ImageFilter.GaussianBlur(2)))]:
            with self.subTest(sample=name):
                with patch.object(extract, '_refine_ocr', side_effect=lambda r:r[0]):
                    before = extract.ocr_image(sample, psm=3)
                after = extract.ocr_image(sample, psm=3)
                self.assertEqual(after, before)
                for protected in ('10:00', '8:00', '2', 'care@example.com', '$25.50'):
                    self.assertIn(protected, after)


def ImageOps_invert_bbox(image):
    from PIL import ImageOps
    return ImageOps.invert(image).getbbox()


if __name__ == '__main__':
    unittest.main()
