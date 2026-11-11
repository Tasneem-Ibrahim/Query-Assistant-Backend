# Adaptive Tesseract OCR

The shared `rag.extract.ocr_image` function now keeps a conservative grayscale,
white-alpha-composited, autocontrasted baseline and only retries weak results.
Upscaling is limited to 4x and a roughly 12 megapixel working image. Retry images
use median denoising, contrast stretching, unsharp masking, small-angle deskew
(up to 8 degrees), and local background thresholding for uneven illumination.
No additional packages are required.

Selection uses character-weighted Tesseract word confidence, the proportion of
reliable characters, and a short-result penalty. A retry must improve the score
by more than two points after discounting reduced character coverage. This prevents a high-confidence partial retry from replacing a fuller result. High quality results exit early; retry timeouts retain
successful text. At most six page engine calls are made, each with a 60-second timeout. Targeted refinement can add up to eight line calls, each limited to 20 seconds.
OCR confidence is a heuristic, not a guarantee that recognised text is correct.
Text is reconstructed from Tesseract line identifiers; exact whitespace may differ.

The return type and page records stay unchanged. RAG, Supabase, remote OCR,
API routes and frontend code are unchanged. Local cache files use `ocr-v5` so
old extraction results are not reused on reprocessing. Already indexed documents
must be explicitly reprocessed to update stored text/vectors. Cloud improvements
apply when `OCR_PROVIDER=tesseract`; API OCR providers are unaffected.

## Validation

Run from the backend with its existing virtual environment:

```powershell
$env:STORAGE_BACKEND='local'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/compare_ocr.py
```

`ocr-comparison.json` contains actual baseline and improved text from Tesseract
5.5.3.20260724. The baseline reproduces the former preprocessing and explicit
PSM 3 path used by image uploads/scanned pages. The metric is normalized character
SequenceMatcher similarity, not word accuracy or character error rate.

| Synthetic sample | Before | After |
| --- | ---: | ---: |
| Clear | 100% | 100% |
| Gaussian blur (radius 3.5) | 100% | 100% |
| Small text | 100% | 100% |
| Skew (6 degrees) | 62.96% | 100% |
| Low contrast, shadow and seeded noise | 0% | 96.26% |

The tests cover confident-image fast paths, explicit-layout retries, confidence
aggregation, inferior-result rejection, retry timeouts, blank output, alpha,
working size bounds, deskew, cache invalidation and real before/after OCR.
The existing image-format, scanned-PDF, local retrieval, mocked cloud and API tests
also run. Live Supabase was not exercised. Test-process local storage avoids the
machine's configured Supabase connection; no `.env` changes are needed.

## Limits

These samples are generated English printed text, not a representative document
corpus. Clear-image preservation is demonstrated on these fixtures only. Strong
blur can permanently erase letters; no sharpening recovers missing information.
Handwriting, perspective distortion, 90-degree rotations, complex tables and
other languages need separate evaluation. Weak inputs take longer due to retries.

The image-processing choices follow the Tesseract quality guidance:
https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html

Initial adaptive validation: 60 backend unittest tests passed (including 11 adaptive OCR
tests); offline RAG ingestion/retrieval passed for both assistants; all 12
frontend tests and frontend JavaScript syntax checks passed. The first run with
machine cloud settings had four Supabase connection errors; rerunning with
process-only local storage passed. The offline RAG smoke test also required an
empty process-only QDRANT_URL to use its temporary embedded database.

Hospital notice regression: candidate selection now discounts dropped character coverage. Tests verify that the final policy line survives both direct image OCR and the cloud PDF-rendering path using the local notice fixture.
