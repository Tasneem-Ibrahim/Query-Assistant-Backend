"""STEP 4 of the diagram: normalise the extracted data with an LLM.

Input per page : Data01 (text) + Data02 (text in image)
Output per page: {"page", "title", "sections": [{"heading", "content"}]}   (no duplication)

Order of work (cheap first, LLM second):
  1. code removes repeated headers / footers  ("Page 3 of 12" ...)
  2. code removes OCR lines that merely repeat the page text (exact-ish duplicates)
  3. the LLM merges what is left, removes meaning-level duplicates, fixes OCR errors,
     and returns clean JSON sections
If the LLM is unavailable or returns something suspicious, a rule-based fallback is used,
so ingestion never breaks.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Callable

from . import config, llm

PROMPT_VERSION = "v2"

SYSTEM_PROMPT = """You are the data-normalisation step of a RAG ingestion pipeline.
You receive the data extracted from ONE page of a PDF in two parts:
- Data01: the text layer of the page.
- Data02: text recognised by OCR inside images on the page (tables, charts, infographics). It may contain OCR mistakes.

Goal: keep only the necessary data, with NO duplication, so it can be chunked and embedded.

Rules:
- If the same fact appears in both Data01 and Data02 (even worded differently), keep it ONCE.
- Keep facts that appear only in Data02 (for example table rows or figures) - never drop them.
- Fix obvious OCR errors and broken words. Use Data01 as the reference for spelling of names and terms.
- Remove page headers, footers and page numbers.
- Rebuild broken lines into proper sentences. Keep headings, numbered lists and bullet lists.
- Write tables as short readable rows, e.g. "Standard | 3-5 business days | $4.99 | cut-off 11:59 PM".
- NEVER add, summarise, translate or invent information. Keep every number, name, date and amount exactly.
- Keep the original language (English / Roman Urdu).
- Split the page into sections by heading. Use an empty heading for text that has no heading.

Return ONLY a JSON object, no markdown fences:
{"title": "short page title", "sections": [{"heading": "...", "content": "..."}]}"""


# --------------------------------------------------------------------------- #
# cheap, code-only steps
# --------------------------------------------------------------------------- #
def _norm_line(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def strip_repeated_lines(pages: list[dict]) -> list[dict]:
    """Remove lines that repeat on at least half of the pages (running headers/footers)."""
    if len(pages) < 4:
        return pages
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update({_norm_line(l) for l in page["text"].splitlines() if l.strip()})
    threshold = max(3, len(pages) // 2)
    repeated = {line for line, n in counts.items() if n >= threshold}
    cleaned = []
    for page in pages:
        lines = [l for l in page["text"].splitlines() if _norm_line(l) not in repeated]
        cleaned.append({**page, "text": "\n".join(lines).strip()})
    return cleaned


def _tokens(text: str) -> list[str]:
    """Words and numbers; single LETTERS are ignored (OCR often turns '3.' into 'G)'), digits are kept."""
    return [t for t in re.findall(r"[a-z0-9$%]+", text.lower()) if len(t) > 1 or t.isdigit()]


def drop_duplicate_image_lines(text: str, image_text: list[str]) -> list[str]:
    """Drop OCR lines whose words are (almost) all already in the page text."""
    page_tokens = set(_tokens(text))
    kept_blocks = []
    for block in image_text:
        kept = []
        for line in block.splitlines():
            toks = _tokens(line)
            if not toks:
                continue
            overlap = sum(t in page_tokens for t in toks) / len(toks)
            numbers_known = all(t in page_tokens for t in toks if any(c.isdigit() for c in t))
            if len(toks) >= 3 and overlap >= 0.85 and numbers_known:
                continue  # every word AND every number is already on the page -> duplicate
            kept.append(line)
        if kept:
            kept_blocks.append("\n".join(kept))
    return kept_blocks


_HEADING_RE = re.compile(r"^(\d+(\.\d+)*\.?\s+\S.*|[A-Z][A-Z0-9 ,&\-()/']{5,})$")


def _is_heading(line: str) -> bool:
    line = line.strip()
    return bool(_HEADING_RE.match(line)) and len(line) <= 80 and not line.endswith((".", ":", ","))


def _reflow(lines: list[str]) -> str:
    """Join visually wrapped lines back into paragraphs."""
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if out and len(out[-1]) >= 70 and not out[-1].endswith((".", ":", "!", "?")) and not _is_heading(line):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return "\n".join(out)


def rule_based_clean(page: dict, image_blocks: list[str]) -> dict:
    """Fallback when the LLM is not available: split on headings, keep unique OCR text."""
    sections: list[dict] = []
    heading, buf = "", []

    def flush():
        content = _reflow(buf)
        if content or heading:
            sections.append({"heading": heading, "content": content})

    for line in page["text"].splitlines():
        if _is_heading(line):
            flush()
            heading, buf = line.strip(), []
        else:
            buf.append(line)
    flush()
    for block in image_blocks:
        sections.append({"heading": "Text from image", "content": block})
    sections = [s for s in sections if s["content"].strip()]
    title = sections[0]["heading"] if sections and sections[0]["heading"] else f"Page {page['page']}"
    return {"page": page["page"], "title": title, "sections": sections}


# --------------------------------------------------------------------------- #
# LLM step
# --------------------------------------------------------------------------- #
def _build_user_message(page: dict, image_blocks: list[str]) -> str:
    data02 = "\n\n".join(image_blocks) if image_blocks else "(none)"
    return (
        f"Page {page['page']}\n\n"
        f"Data01 (text layer):\n<<<\n{page['text'] or '(empty)'}\n>>>\n\n"
        f"Data02 (text inside images, OCR):\n<<<\n{data02}\n>>>"
    )


def _valid(result: dict) -> list[dict] | None:
    sections = result.get("sections")
    if not isinstance(sections, list):
        return None
    cleaned = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        content = str(sec.get("content") or "").strip()
        if content:
            cleaned.append({"heading": str(sec.get("heading") or "").strip(), "content": content})
    return cleaned or None


def _load_cache() -> dict:
    path = config.CACHE_DIR / "llm_cache.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_cache(cache: dict) -> None:
    (config.CACHE_DIR / "llm_cache.json").write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def clean_pages(
    raw_pages: list[dict],
    use_llm: bool = True,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Normalise all pages. Returns [{"page", "title", "sections"}]."""
    config.ensure_dirs()
    say = progress or (lambda _msg: None)
    pages = strip_repeated_lines(raw_pages)
    cache = {} if force else _load_cache()
    cache_dirty = False
    llm_ok = use_llm
    results: list[dict] = []

    for page in pages:
        image_blocks = drop_duplicate_image_lines(page["text"], page.get("image_text", []))
        if not page["text"] and not image_blocks:
            say(f"page {page['page']}: empty, skipped")
            continue

        key = hashlib.sha256(
            json.dumps([config.DEEPSEEK_MODEL, PROMPT_VERSION, page["text"], image_blocks], ensure_ascii=False).encode()
        ).hexdigest()

        cleaned = None
        if llm_ok:
            if key in cache:
                cleaned = cache[key]
                say(f"page {page['page']}: LLM result from cache")
            else:
                try:
                    reply = llm.chat_json(SYSTEM_PROMPT, _build_user_message(page, image_blocks), max_tokens=4000)
                    sections = _valid(reply)
                    in_chars = len(page["text"]) + sum(len(b) for b in image_blocks)
                    out_chars = sum(len(s["content"]) for s in sections) if sections else 0
                    if sections and out_chars >= 0.3 * in_chars:
                        cleaned = {"title": str(reply.get("title") or "").strip(), "sections": sections}
                        cache[key], cache_dirty = cleaned, True
                        say(f"page {page['page']}: cleaned by LLM ({len(sections)} sections)")
                    else:
                        say(f"page {page['page']}: LLM output looked incomplete, using rule-based cleaning")
                except llm.LLMNotConfigured as exc:
                    say(f"{exc} Continuing WITHOUT the LLM cleaning step.")
                    llm_ok = False
                except Exception as exc:
                    say(f"page {page['page']}: LLM failed ({exc}); using rule-based cleaning")
        if cleaned is None:
            result = rule_based_clean(page, image_blocks)
        else:
            result = {"page": page["page"], "title": cleaned["title"] or f"Page {page['page']}", "sections": cleaned["sections"]}
        results.append(result)

    if cache_dirty:
        _save_cache(cache)
    return results
