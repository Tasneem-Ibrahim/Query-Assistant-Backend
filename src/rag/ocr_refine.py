"""Conservative, spatial OCR refinement. No dictionaries or spelling correction."""
from __future__ import annotations

from difflib import SequenceMatcher
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

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


def _uncovered_footer(result):
    """Recover visually present text below all recognized words, never guess it.

    A continuous decoration crossing the recognized-text boundary is excluded
    from the crop. Two segmentation modes must agree at high word confidence.
    """
    lines = _lines(result.data)
    words = [word for line in lines.values() for word in line]
    if not words:
        return ''
    image = result.image.convert('L')
    pixels = np.asarray(image)
    height, width = pixels.shape
    typical = max(8, int(np.median([word.height for word in words])))
    bottom = max(word.top + word.height for word in words)
    top = bottom + max(2, typical // 20)
    if height - top < max(12, typical // 2):
        return ''
    background = int(np.bincount(pixels.ravel(), minlength=256).argmax())
    ink = np.abs(pixels.astype('int16') - background) > 40
    edge = max(2, min(width, height) // 150)
    ink[:edge] = ink[-edge:] = False
    ink[:, :edge] = ink[:, -edge:] = False
    ys = np.flatnonzero(ink[top:].sum(axis=1) >= max(5, width // 200))
    if not ys.size:
        return ''
    last = top + int(ys[-1]) + 1
    # A thin vertical rule persists from above the old text into the missed area.
    crossing = ink[max(edge, bottom - typical // 2):last].mean(axis=0) > .90
    boundaries = np.flatnonzero(np.diff(np.r_[False, crossing, False]))
    for left, right in zip(boundaries[::2], boundaries[1::2]):
        if right - left < typical / 4 and last - top > typical:
            ink[top:, left:right] = False
    yy, xx = np.nonzero(ink[top:])
    if len(xx) < max(40, width * typical // 25):
        return ''
    first, last = top + int(yy.min()), top + int(yy.max()) + 1
    left, right = int(xx.min()), int(xx.max()) + 1
    if last - first < typical / 3 or right - left < typical / 2:
        return ''
    padding = max(10, typical // 10)
    crop = image.crop((max(0,left-padding), max(top,first-padding), min(width,right+padding), min(height,last+padding)))
    crop = ImageOps.expand(crop,border=padding,fill=background)
    import pytesseract
    reads = []
    try:
        for mode in (6, 11):
            data = pytesseract.image_to_data(crop,lang=config.OCR_LANG,config=f'--psm {mode}',
                output_type=pytesseract.Output.DICT,timeout=20)
            found = [word for line in _lines(data).values() for word in line]
            if not found or min(word.confidence for word in found) < 85:
                return ''
            reads.append(' '.join(word.text for word in found))
    except RuntimeError:
        return ''
    text = reads[0]
    if text != reads[1] or sum(char.isalnum() for char in text) < 2:
        return ''
    if text.casefold() in ' '.join(result[0].split()).casefold():
        return ''
    return text


def refine_ocr(result):
    """Refine four uncertain lines and verify an uncovered footer when present.

    Existing lines are never dropped, merged or reordered; a confirmed footer
    can be appended using at most two extra calls. Confident words, digits,
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
    try:
        footer = _uncovered_footer(result)
    except (ValueError, RuntimeError):
        footer = ''
    if footer:
        output.append(footer)
    return '\n'.join(output)
