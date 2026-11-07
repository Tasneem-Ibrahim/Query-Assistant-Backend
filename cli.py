"""Command line interface.

    python cli.py ingest                              # process the default PDFs of BOTH assistants (once)
    python cli.py ingest -d legal_assistant           # one assistant only
    python cli.py ingest -d legal_assistant --pdf my.pdf
    python cli.py ingest --force                      # rebuild even if already processed
    python cli.py ask -d customer_support "How long does express delivery take?"
    python cli.py chat -d legal_assistant             # interactive chat
    python cli.py search -d customer_support "refund timeline"   # raw chunks, no LLM
    python cli.py reset -d legal_assistant            # delete one assistant's vectors (then ingest again)
    python cli.py status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from rag import answer, ingest  # noqa: E402
from rag.domains import DOMAINS  # noqa: E402


def _domain_arg(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument(
        "-d", "--domain", choices=list(DOMAINS), required=required,
        help="which assistant: " + ", ".join(DOMAINS),
    )


def cmd_ingest(args) -> None:
    keys = [args.domain] if args.domain else list(DOMAINS)
    for key in keys:
        print(f"\n=== {DOMAINS[key].icon} {DOMAINS[key].label} ===")
        if args.pdf:
            reports = [ingest.ingest_pdf(key, args.pdf, force=args.force, use_llm=not args.no_llm, progress=print)]
        else:
            reports = ingest.ingest_default_pdfs(key, force=args.force, use_llm=not args.no_llm, progress=print)
        for r in reports:
            print(f"-> {r['status']}: {r['name']} ({r['pages']} pages, {r['chunks']} chunks)")


def _print_answer(result: dict, show_context: bool = False) -> None:
    print("\n" + result["answer"])
    if result["sources"]:
        print("\nSources:")
        for hit in result["sources"]:
            m = hit["metadata"]
            print(f"  - {m['source']}, page {m['page']}" + (f", {m['heading']}" if m.get("heading") else ""))
    if show_context:
        print("\n--- retrieved chunks ---")
        for i, hit in enumerate(result["hits"], 1):
            print(f"[{i}] score={hit['score']:.3f} page={hit['metadata']['page']}\n{hit['text']}\n")


def cmd_ask(args) -> None:
    result = answer.ask(args.domain, " ".join(args.question), top_k=args.top_k)
    _print_answer(result, args.show_context)


def cmd_chat(args) -> None:
    domain = DOMAINS[args.domain]
    print(f"{domain.icon} {domain.label} - type 'exit' to quit.\n{domain.welcome}\n")
    history: list[dict] = []
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            break
        result = answer.ask(domain, question, history=history)
        _print_answer(result)
        history += [{"role": "user", "content": question}, {"role": "assistant", "content": result["answer"]}]
        print()


def cmd_search(args) -> None:
    for i, hit in enumerate(answer.retrieve(args.domain, " ".join(args.query), args.top_k), 1):
        m = hit["metadata"]
        print(f"[{i}] score={hit['score']:.3f}  {m['source']} p.{m['page']}  {m.get('heading', '')}\n{hit['text']}\n")


def cmd_reset(args) -> None:
    ingest.reset_domain(args.domain)
    print(f"{DOMAINS[args.domain].label}: vector database cleared. Run 'python cli.py ingest -d {args.domain}' again.")


def cmd_status(_args) -> None:
    for key, domain in DOMAINS.items():
        info = ingest.status(key)
        print(f"{domain.icon} {domain.label}: {info['chunks']} chunks")
        for h, f in info["files"].items():
            print(f"    {f['name']}  ({f['pages']} pages, {f['chunks']} chunks, {f['ingested_at']})")


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF RAG assistants (Customer Support / Legal Assistant)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="process PDFs ONCE: extract, clean, chunk, embed, store")
    _domain_arg(p, required=False)
    p.add_argument("--pdf", help="path to a PDF (default: the PDFs in pdfs/<assistant>/)")
    p.add_argument("--force", action="store_true", help="rebuild even if this PDF was already processed")
    p.add_argument("--no-llm", action="store_true", help="skip the DeepSeek cleaning step (rule-based cleaning)")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ask", help="ask one question")
    _domain_arg(p, required=True)
    p.add_argument("question", nargs="+")
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--show-context", action="store_true")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("chat", help="interactive chat in the terminal")
    _domain_arg(p, required=True)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("search", help="show the raw retrieved chunks (no LLM)")
    _domain_arg(p, required=True)
    p.add_argument("query", nargs="+")
    p.add_argument("--top-k", type=int, default=5)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("reset", help="delete one assistant's vectors (e.g. after changing the embedding model)")
    _domain_arg(p, required=True)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("status", help="show what is in the vector database")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
