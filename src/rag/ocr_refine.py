"""Conservative, spatial OCR refinement. No dictionaries or spelling correction."""
from __future__ import annotations

from difflib import SequenceMatcher
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from PIL import ImageFilter, ImageOps

from . import config


@dataclass
class Word:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int


def _lines(data):
    lines = defaultdict(list)
    if not all(key in data for key in ('left', 'top', 'width', 'height')):
        return lines
    for i, text in enumerate(data['text']):
        if text.strip() and float(data['conf'][i]) >= 0:
            key = tuple(data[field][i] for field in ('page_num', 'block_num', 'par_num', 'line_num'))
            lines[key].append(Word(text.strip(), float(data['conf'][i]),
                                   *(int(data[field][i]) for field in ('left', 'top', 'width', 'height'))))
    return lines


def _filled_bullet(image, word):
    """A detached filled disk, not an @ glyph or a punctuation blacklist."""
    if word.text not in {'@', 'e', 'e@', '•', '\ufffd', '*'} or not 0.8 <= word.width / max(word.height, 1) <= 1.2:
        return False
    pixels = np.asarray(image.crop((word.left, word.top, word.left + word.width, word.top + word.height)).convert('L'))
    if pixels.size < 25 or pixels.max() - int(pixels.min()) < 35:
        return False
    ink = pixels < (float(pixels.min()) + float(pixels.max())) / 2
    h, w = ink.shape
    center = ink[h//3:2*h//3, w//3:2*w//3]
    corners = [ink[:max(1,h//5), :max(1,w//5)], ink[:max(1,h//5), -max(1,w//5):],
               ink[-max(1,h//5):, :max(1,w//5)], ink[-max(1,h//5):, -max(1,w//5):]]
    return .55 <= ink.mean() <= .90 and center.mean() > .95 and all(c.mean() < .4 for c in corners)


def _bullet_indices(image, lines):
    candidates = []
    for key, words in lines.items():
        if len(words) < 3:
            continue
        first, second = words[:2]
        body_height = float(np.median([w.height for w in words[1:]]))
        if (first.height <= body_height * .85 and
                second.left - first.left - first.width >= body_height * .3 and
                _filled_bullet(image, first)):
            candidates.append((key, first))
    # Repeated aligned disks are much stronger evidence than one isolated symbol.
    return {key for key, word in candidates if sum(
        abs(word.left - other.left) <= max(word.width, other.width) * .3
        and abs(word.top - other.top) > max(word.height, other.height)
        for _, other in candidates) >= 1}


def _replacement(original, first, second):
    """Two visual reads must agree; preserve strong words and protected tokens."""
    if original.confidence >= 90 or any(char.isdigit() for char in original.text):
        return original.text
    if first.text != second.text or min(first.confidence, second.confidence) < max(85, original.confidence + 5):
        return original.text
    # Keep punctuation/case structure; a replacement glyph may represent one letter.
    unknown = original.text == '\ufffd'
    if unknown:
        return first.text if len(first.text) == 1 and first.text.isalpha() else original.text
    punctuation = lambda text: ''.join(char for char in text if not char.isalnum())
    if punctuation(original.text) != punctuation(first.text):
        return original.text
    if any(char.isdigit() for char in first.text):
        return original.text
    if not original.text.isascii() or not first.text.isascii():
        return original.text
    if SequenceMatcher(None, original.text, first.text).ratio() < .7:
        return original.text
    if abs(len(first.text) - len(original.text)) > 1:
        return original.text
    if original.text[:1].isupper() != first.text[:1].isupper():
        return original.text
    return first.text


def _read_line(image):
    import pytesseract
    data = pytesseract.image_to_data(image, lang=config.OCR_LANG, config='--psm 7',
                                     output_type=pytesseract.Output.DICT, timeout=20)
    return [word for words in _lines(data).values() for word in words]


def refine_ocr(result):
    """Refine at most four uncertain lines (eight additional bounded OCR calls).

    No line is dropped, merged or reordered. Unchanged confident words, digits,
    punctuation and failed/disagreeing retries are retained verbatim.
    """
    if not hasattr(result, 'data'):
        return result[0]
    lines = _lines(result.data)
    if not lines:
        return result[0]
    image = result.image
    bullets = _bullet_indices(image, lines)
    retries = 0
    output = []
    for key, words in lines.items():
        texts = [word.text for word in words]
        start = 1 if key in bullets else 0
        body = words[start:]
        if retries < 4 and any(w.confidence < 90 and not any(c.isdigit() for c in w.text) for w in body):
            # Crop the whole line so neighbouring letters retain context.
            left = min(w.left for w in body)
            top = min(w.top for w in body)
            right = max(w.left + w.width for w in body)
            bottom = max(w.top + w.height for w in body)
            padding = max(8, (bottom - top) // 6)
            origin = max(0, left - padding)
            crop = ImageOps.expand(image.crop((origin, max(0, top - padding),
                min(image.width, right + padding), min(image.height, bottom + padding))), border=10, fill=255)
            retries += 1
            try:
                first = _read_line(crop)
                second = _read_line(crop.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3)))
                aligned = len(first) == len(second) == len(body)
                if aligned:
                    aligned = all(
                        a.text == b.text == original.text
                        for original, a, b in zip(body, first, second)
                        if original.confidence >= 90 or any(c.isdigit() for c in original.text))
                if aligned:
                    for i, (original, a, b) in enumerate(zip(body, first, second), start):
                        expected = original.left - origin + 10 + original.width / 2
                        tolerance = max(original.height, original.width * .4)
                        if all(abs(w.left + w.width / 2 - expected) <= tolerance for w in (a, b)):
                            texts[i] = _replacement(original, a, b)
            except RuntimeError:
                pass
        if key in bullets:
            texts[0] = '•'
        output.append(' '.join(texts))
    return '\n'.join(output)
