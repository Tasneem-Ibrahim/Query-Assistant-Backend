"""Reproducible synthetic OCR comparison; no external services or documents."""
import json
import re
import sys
from pathlib import Path
from difflib import SequenceMatcher

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
import numpy as np
import pytesseract

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rag import config, extract

TEXT = 'Returns accepted within thirty days.\nKeep your receipt for a full refund.\nContact support for delivery updates.'


def samples():
    image = Image.new('RGB', (1500, 480), 'white')
    font = ImageFont.load_default(size=44)
    ImageDraw.Draw(image).multiline_text((60, 100), TEXT, font=font, fill='black', spacing=30)
    yield 'clear', image
    yield 'blur', image.filter(ImageFilter.GaussianBlur(3.5))
    yield 'small', image.resize((600, 192))
    yield 'skew', image.rotate(6, expand=True, fillcolor='white')
    gray = np.asarray(image.convert('L'), dtype=float)
    gradient = np.linspace(0.25, 1, image.width)[None, :]
    noisy = np.clip((180 + gray * .22) * gradient + np.random.default_rng(42).normal(0, 5, gray.shape), 0, 255)
    yield 'shadow_noise', Image.fromarray(noisy.astype('uint8'))


def baseline(image):
    image = image.convert('RGB').convert('L')
    if image.width < 1800:
        scale = 1800 / image.width
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)
    return pytesseract.image_to_string(ImageOps.autocontrast(image), lang=config.OCR_LANG, config='--psm 3', timeout=60)


def normalize(text):
    return ' '.join(re.findall(r'\w+', text.lower()))


def compare():
    pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd()
    rows = []
    for name, image in samples():
        before, after = baseline(image), extract.ocr_image(image, psm=3)
        score = lambda text: round(100 * SequenceMatcher(None, normalize(TEXT), normalize(text)).ratio(), 2)
        rows.append(dict(sample=name, before_similarity=score(before), after_similarity=score(after), before_text=before.strip(), after_text=after))
    return dict(engine=str(pytesseract.get_tesseract_version()), metric='Normalized character SequenceMatcher similarity (%), synthetic English samples', results=rows)


if __name__ == '__main__':
    print(json.dumps(compare(), indent=2))
