# Targeted OCR refinement

Implemented in `src/rag/ocr_refine.py`, called only by the shared Tesseract
function in `extract.py`. No RAG, Supabase, API or frontend source was changed.

## Safeguards

- Retain word coordinates, confidence and the selected image internally; the
  public OCR result remains a string with the same line order.
- Retry up to four uncertain lines using padded crops, PSM 7 and two image
  variants (original crop and mild sharpening). At most eight extra engine
  calls, with a 20-second timeout per call.
- Both visual reads must agree, have at least 85 confidence, and improve the
  original confidence by at least five points. Word count and spatial alignment
  must match. Confident or numeric context must match before any line edits.
- Never replace words with confidence >=90, tokens containing digits, or change
  punctuation. Other alphabetic edits must be close to the original. The Unicode
  replacement character U+FFFD may be replaced by one visually confirmed letter.
  Literal guillemets are preserved because they can be valid punctuation.
- Normalize a leading token to a bullet only when its image is a filled disk,
  separated from body text, smaller than the text, and aligned with another disk
  on a different line. A real @ glyph, inline symbol or isolated disk is retained.
- No spelling dictionaries, guessed corrections, global symbol deletion, line
  removal or line reordering. Retry timeouts retain the original text.
- Extraction cache version is now `ocr-v5`. Existing indexed documents need
  explicit reprocessing; stored previews and vectors are not silently changed.

## Before/after comparison

Run `python scripts/compare_targeted_ocr.py` from the backend virtual environment.
The script captures the selected OCR output immediately before refinement, then
refines that exact result. Thus both sides use identical page OCR recognition.
The complete text and manually transcribed references are saved in
`ocr-targeted-comparison.json`.

Metric: Levenshtein character error rate after whitespace normalization,
case/punctuation retained. Lower is better. Tesseract version: 5.5.3.20260724.

| Sample | Before CER | After CER |
| --- | ---: | ---: |
| Clear text with timings, email, price and punctuation | 0.00% | 0.00% |
| The same text with Gaussian blur (radius 2) | 0.00% | 0.00% |
| Local hospital visiting notice | 6.70% | 5.15% |

All line counts and numeric token sequences were retained. On the hospital
notice the three icon-derived @ tokens become bullets. `quict`, the literal
`<<`-like guillemet, and ambiguous `oF)` / `ml)` graphics remain. Although cropped
reads suggest `quiet`, their confidence is below the existing reading, so the
replacement is rejected. Literal punctuation and ambiguous graphics are retained
to avoid deleting meaningful content. An early experiment recovered `a` from
the guillemet; the final stricter punctuation policy deliberately rejects that edit.

## Validation and limits

Tests cover clear and blurred OCR, agreeing/disagreeing retries, protected
context, numerical tokens, punctuation, confidence thresholds, unrelated words,
spatial mismatch, token count changes, timeouts, retry budgets, real @ glyphs,
inline symbols, isolated disks and aligned bullets. Existing tests cover the
hospital footer through direct image and cloud-style PDF rendering.

The corpus is small and contains English printed text; this is not a guarantee
for arbitrary documents. Two variants of the same OCR engine can share errors.
Punctuation preservation intentionally leaves some incorrect glyphs unchanged.
Large or ambiguous icons remain rather than risk deleting text. More than four
uncertain lines can remain untreated on a page; retries add processing time.
Multi-column whitespace and table alignment retain the existing OCR limitations.
The cloud API OCR provider is unchanged; refinement applies to Tesseract only.
Live Supabase was not accessed for validation.

Final validation: all 70 backend unittest tests passed (including 10 targeted
refinement tests), both offline RAG ingestion/retrieval checks passed, all 12
frontend tests passed, and frontend JavaScript syntax checks passed. Backend
tests used process-only local storage settings. No project environment file
was modified.
