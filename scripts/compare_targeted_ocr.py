"""Compare the previous adaptive OCR with targeted refinement on identical reads."""
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont, ImageFilter
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rag import config, extract


def character_error_rate(reference, text):
    reference, text = (' '.join(value.split()) for value in (reference, text))
    row = list(range(len(text) + 1))
    for i, a in enumerate(reference, 1):
        next_row = [i]
        for j, b in enumerate(text, 1):
            next_row.append(min(next_row[-1]+1, row[j]+1, row[j-1]+(a != b)))
        row = next_row
    return round(100 * row[-1] / max(1, len(reference)), 2)


def cases():
    reference = 'Visiting Hours: 10:00 AM to 8:00 PM.\nMaximum of 2 visitors per patient.\nEmail: care@example.com; Fee: $25.50.'
    image = Image.new('RGB', (1500, 400), 'white')
    ImageDraw.Draw(image).multiline_text((50,60), reference, font=ImageFont.load_default(size=40), fill='black', spacing=25)
    yield 'clear_protected_text', image, reference
    yield 'blurred_protected_text', image.filter(ImageFilter.GaussianBlur(2)), reference
    path = Path(__file__).resolve().parents[2] / 'documents' / 'Hospital Visiting Policy Notice.png'
    if path.exists():
        with Image.open(path) as hospital:
            reference = 'Hospital Visiting Policy\nVisiting Hours:\nDaily from 10:00 AM to 8:00 PM.\nGuidelines:\n\u2022 Maximum of 2 visitors per patient.\n\u2022 Visitors must wear a face mask.\n\u2022 Please maintain a quiet environment.'
            yield 'hospital_notice', hospital.copy(), reference


def compare():
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd()
    rows = []
    for name, image, reference in cases():
        # Capture the selected result before refinement within the SAME OCR run.
        selected = []
        refine = extract._refine_ocr
        def capture(result):
            selected.append(result[0])
            return refine(result)
        with patch.object(extract, '_refine_ocr', side_effect=capture):
            after = extract.ocr_image(image, psm=3)
        before = selected[-1]
        rows.append(dict(sample=name, reference=reference, before_text=before, after_text=after,
                         before_cer_percent=character_error_rate(reference, before),
                         after_cer_percent=character_error_rate(reference, after),
                         before_lines=len(before.splitlines()), after_lines=len(after.splitlines()),
                         numeric_tokens_preserved=re.findall(r'\d+(?:[:.]\d+)*', before) == re.findall(r'\d+(?:[:.]\d+)*', after)))
    return dict(engine=str(pytesseract.get_tesseract_version()), metric='Character error rate (%), whitespace normalized, case/punctuation preserved; lower is better', results=rows)


if __name__ == '__main__':
    report = compare()
    output = Path(__file__).resolve().parents[1] / 'ocr-targeted-comparison.json'
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding='utf-8')
    for row in report['results']:
        print(row['sample'], row['before_cer_percent'], '->', row['after_cer_percent'], 'numeric tokens preserved:', row['numeric_tokens_preserved'])
