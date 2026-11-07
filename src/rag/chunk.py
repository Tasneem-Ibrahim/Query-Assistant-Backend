"""STEP 5: structure-aware chunking.

- Chunks never cross a page boundary, so every answer can cite an exact page.
- Small neighbouring sections are packed together; big sections are split on
  sentence boundaries with a small overlap.
- Each chunk keeps its heading, so it still makes sense when read alone.
"""

from __future__ import annotations

import re
import uuid

from . import config

_NAMESPACE = uuid.UUID("5d0c1a4e-6b55-4a73-9f3a-2d1e8c7b9a10")
MIN_MERGE_CHARS = 400  # only merge a section into the previous chunk while that chunk is still small


def _units(text: str) -> list[str]:
    """Split text into sentence-like units (lines first, then sentences inside long lines)."""
    units: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) > 300:
            units.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip())
        else:
            units.append(line)
    return units


def split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split long text into parts of about max_chars, overlapping by about `overlap` chars."""
    units = _units(text)
    parts: list[list[str]] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and size + len(unit) + 1 > max_chars:
            parts.append(current)
            tail: list[str] = []
            tail_size = 0
            for u in reversed(current):  # carry the last few units into the next part
                if tail_size + len(u) > overlap:
                    break
                tail.insert(0, u)
                tail_size += len(u) + 1
            current, size = tail, tail_size
        current.append(unit)
        size += len(unit) + 1
    if current:
        parts.append(current)
    return ["\n".join(p) for p in parts]


def _block_text(heading: str, content: str) -> str:
    return f"{heading}\n{content}" if heading else content


def chunk_pages(clean_pages: list[dict], domain: str, source: str, file_hash: str) -> list[dict]:
    max_chars = config.CHUNK_CHARS
    chunks: list[dict] = []

    for page in clean_pages:
        pending: list[tuple[str, str]] = []  # (headings, text) groups for this page
        buffer_text, buffer_headings = "", []

        def flush():
            nonlocal buffer_text, buffer_headings
            if buffer_text.strip():
                pending.append((" / ".join(h for h in buffer_headings if h), buffer_text.strip()))
            buffer_text, buffer_headings = "", []

        for section in page["sections"]:
            heading, content = section["heading"], section["content"]
            block = _block_text(heading, content)
            if len(block) > max_chars:
                flush()
                for part in split_text(content, max(200, max_chars - len(heading) - 1), config.CHUNK_OVERLAP_CHARS):
                    pending.append((heading, _block_text(heading, part)))
            elif buffer_text and len(buffer_text) < MIN_MERGE_CHARS and len(buffer_text) + len(block) + 2 <= max_chars:
                buffer_text += "\n\n" + block
                buffer_headings.append(heading)
            else:
                flush()
                buffer_text, buffer_headings = block, [heading]
        flush()

        for index, (heading, text) in enumerate(pending):
            chunks.append(
                {
                    "id": str(uuid.uuid5(_NAMESPACE, f"{domain}:{file_hash}:{page['page']}:{index}")),
                    "text": text,
                    "metadata": {
                        "domain": domain,
                        "source": source,
                        "file_hash": file_hash,
                        "page": page["page"],
                        "title": page.get("title", ""),
                        "heading": heading,
                        "chunk_index": index,
                    },
                }
            )
    return chunks
