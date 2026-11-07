"""Query time (the right half of the diagram):
User question -> embedding -> similarity search in Qdrant -> LLM answer from the retrieved chunks.

The PDF is never re-processed here; only the vector database is read.
"""

from __future__ import annotations

from . import config, embed, llm, vectorstore
from .domains import Domain, get_domain, system_prompt

REWRITE_PROMPT = """Rewrite the user's last message as a short standalone search query, using the chat history to resolve
words like "it", "that", "wo", "iska". Keep the same language. Do not answer the question.
Return ONLY JSON: {"query": "..."}"""


def retrieve(domain: Domain | str, query: str, top_k: int | None = None) -> list[dict]:
    domain = get_domain(domain) if isinstance(domain, str) else domain
    vector = embed.get_embedder().embed_query(query)
    return vectorstore.search(domain.collection, vector, top_k or config.TOP_K)


def _standalone_query(question: str, history: list[dict]) -> str:
    """Make follow-up questions searchable ("and for electronics?" -> full question). Falls back to the raw text."""
    if not history:
        return question
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
    try:
        reply = llm.chat_json(REWRITE_PROMPT, f"Chat history:\n{transcript}\n\nLast message: {question}", max_tokens=150, retries=2)
        return str(reply.get("query") or "").strip() or question
    except Exception:
        return question


def build_context(hits: list[dict]) -> str:
    blocks = []
    for number, hit in enumerate(hits, start=1):
        meta = hit["metadata"]
        where = f"{meta.get('source', '?')}, page {meta.get('page', '?')}"
        if meta.get("heading"):
            where += f", section: {meta['heading']}"
        blocks.append(f"[{number}] ({where})\n{hit['text']}")
    return "\n\n".join(blocks)


def _parse(raw_reply: dict, hits: list[dict]) -> tuple[str, list[dict]]:
    answer = str(raw_reply.get("answer") or "").strip()
    used = []
    for number in raw_reply.get("sources", []) or []:
        if isinstance(number, int) and 1 <= number <= len(hits) and hits[number - 1] not in used:
            used.append(hits[number - 1])
    return answer, used


def ask(
    domain: Domain | str,
    question: str,
    history: list[dict] | None = None,
    top_k: int | None = None,
) -> dict:
    """Answer one question. Returns {'answer', 'sources', 'query', 'hits'}.

    history: previous turns as [{'role': 'user'|'assistant', 'content': str}, ...] (optional)
    """
    domain = get_domain(domain) if isinstance(domain, str) else domain
    history = history or []

    query = _standalone_query(question, history)
    hits = retrieve(domain, query, top_k)
    if not hits:
        return {
            "answer": "The knowledge base is empty. Please process the PDF first (python cli.py ingest, or the button in the app).",
            "sources": [],
            "query": query,
            "hits": [],
        }

    user_message = f"Question: {query}\n\nDocument excerpts:\n\n{build_context(hits)}"
    reply = llm.chat_json(system_prompt(domain), user_message, max_tokens=700)
    answer, used = _parse(reply, hits)
    if not answer:
        answer = "I could not produce an answer from the documents."
    return {"answer": answer, "sources": used, "query": query, "hits": hits}
