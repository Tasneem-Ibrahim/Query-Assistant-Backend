"""The two assistants (domains). Each has its own PDFs, its own Qdrant collection
and its own answering style."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import shutil
import threading
import uuid

from . import config


@dataclass(frozen=True)
class Domain:
    key: str
    label: str
    icon: str
    collection: str
    pdf_subdir: str
    persona: str
    welcome: str
    examples: list[str] = field(default_factory=list)

    @property
    def pdf_dir(self):
        return config.PDF_DIR / self.pdf_subdir

    def default_pdfs(self):
        SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp", ".bmp", ".tif", ".tiff", ".avif"}
        return sorted(p for p in self.pdf_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS) if self.pdf_dir.exists() else []


COMMON_RULES = """Rules:
- Use ONLY the numbered document excerpts given to you. Never add outside facts, numbers or policies.
- Match the language of the current question, even if earlier messages use a different language.
- For an English question, answer in English. For a Roman Urdu question, answer in Roman Urdu using Latin letters, not Urdu script. Do not add a second translation unless requested.
- Be clear and concise (normally 2-5 sentences). Use a short list only when the answer has several steps or items.
- If the excerpts do not contain the answer, say so plainly and do not guess.
- Return ONLY a valid JSON object with no markdown fences:
  {"answer": "...", "sources": [1, 2]}
  "sources" lists only the excerpt numbers you actually used (empty list if the answer is not in the excerpts)."""

CUSTOMER_SUPPORT = Domain(
    key="customer_support",
    label="Customer Support",
    icon="🎧",
    collection="customer_support",
    pdf_subdir="customer_support",
    persona=(
        "You are a friendly, professional customer support assistant for the company described in the "
        "documents. Give practical, step-by-step help. If the issue cannot be solved from the documents, "
        "tell the customer which support channel or escalation level to use, only if the documents mention it."
    ),
    welcome="Ask about orders, delivery, returns, warranties, or your account.",
    examples=[
        "How long does standard delivery take and what does it cost?",
        "How can I cancel an order?",
        "What is the refund timeline for card payments?",
        "How do I escalate a complaint and how fast will I get a reply?",
    ],
)

LEGAL_ASSISTANT = Domain(
    key="legal_assistant",
    label="Legal Assistant",
    icon="⚖️",
    collection="legal_assistant",
    pdf_subdir="legal_assistant",
    persona=(
        "You are a careful legal document assistant. Explain what the uploaded contract says in plain "
        "language and mention the clause number or page when the excerpts show it. You explain the "
        "document; you do not give legal advice, and for decisions with legal consequences you remind the "
        "user to consult a qualified lawyer (keep that reminder to one short sentence)."
    ),
    welcome="Explore your contract's clauses, fees, termination terms, and obligations.",
    examples=[
        "How can the Client terminate the agreement and is there a fee?",
        "What is the liability cap and what are the exceptions?",
        "What are the service credits if availability drops below 99%?",
        "What is the notice period for termination?",
    ],
)

DOMAINS: dict[str, Domain] = {d.key: d for d in (CUSTOMER_SUPPORT, LEGAL_ASSISTANT)}
_BUILTIN_KEYS = frozenset(DOMAINS)
_domain_lock = threading.RLock()


def _custom_domain(key: str, label: str) -> Domain:
    return Domain(
        key=key, label=label, icon="📚", collection=key, pdf_subdir=key,
        persona=("You are a general document research assistant for PDFs on any topic. "
                 "Explain the selected knowledge base's documents clearly and accurately. "
                 "Support your answer with the provided excerpts and page references."),
        welcome="Ask a question about your documents to get started.",
        examples=["What are the main topics in these documents?",
                  "Summarize the key points.",
                  "What are the most important details?",
                  "What conclusions do these documents support?"],
    )


def _custom_path():
    return config.DATA_DIR / "custom_domains.json"


def _read_custom() -> dict[str, str]:
    if getattr(config, "CLOUD_MODE", False):
        from . import cloud
        return {row["key"]: row["label"] for row in cloud.rows("qa_domains", order="key.asc")}
    path = _custom_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid custom knowledge base registry.")
    for key, label in data.items():
        if (not isinstance(key, str) or not key.startswith("custom_")
                or len(key) != 39 or any(c not in "0123456789abcdef" for c in key[7:])
                or not isinstance(label, str) or not label.strip() or len(label) > 80):
            raise ValueError("Invalid custom knowledge base registry entry.")
    return data


def _save_custom(data: dict[str, str]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _custom_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def list_domains() -> list[Domain]:
    with _domain_lock:
        custom = _read_custom()
        for key in list(DOMAINS):
            if key not in _BUILTIN_KEYS and key not in custom:
                DOMAINS.pop(key)
        for key, label in custom.items():
            DOMAINS[key] = _custom_domain(key, label)
        domains = list(DOMAINS.values())
        if getattr(config, "CLOUD_MODE", False):
            domains = [replace(domain, collection=f"{config.embedding_namespace()}_{domain.key}") for domain in domains]
        return domains


def _validate_label(label: str, custom: dict[str, str], excluding: str | None = None) -> str:
    label = label.strip()
    if not label or len(label) > 80 or any(ord(c) < 32 for c in label):
        raise ValueError("Choose a name between 1 and 80 characters without control characters.")
    names = [DOMAINS[key].label for key in _BUILTIN_KEYS]
    names.extend(value for key, value in custom.items() if key != excluding)
    if any(name.casefold() == label.casefold() for name in names):
        raise ValueError("A knowledge base with this name already exists. Choose another name.")
    return label


def create_domain(label: str) -> Domain:
    with _domain_lock:
        custom = _read_custom()
        label = _validate_label(label, custom)
        key = "custom_" + uuid.uuid4().hex
        custom[key] = label
        if getattr(config, "CLOUD_MODE", False):
            from . import cloud
            cloud.write("qa_domains", {"key": key, "label": label}, "key")
        else:
            _save_custom(custom)
        DOMAINS[key] = _custom_domain(key, label)
        return get_domain(key)


def rename_domain(key: str, label: str) -> Domain:
    with _domain_lock:
        custom = _read_custom()
        if key not in custom:
            raise ValueError("Only custom knowledge bases can be renamed.")
        custom[key] = _validate_label(label, custom, excluding=key)
        if getattr(config, "CLOUD_MODE", False):
            from . import cloud
            cloud.write("qa_domains", {"key": key, "label": custom[key]}, "key")
        else:
            _save_custom(custom)
        DOMAINS[key] = _custom_domain(key, custom[key])
        return get_domain(key)


def get_domain(key: str) -> Domain:
    available = {domain.key: domain for domain in list_domains()}
    try:
        return available[key]
    except KeyError:
        raise ValueError(f"Unknown domain '{key}'. Choose one of: {', '.join(DOMAINS)}") from None


def delete_domain(key: str) -> None:
    """Remove a custom base and only its own stored documents and vectors."""
    from . import ingest

    with _domain_lock:
        custom = _read_custom()
        if key not in custom:
            raise ValueError("Only custom knowledge bases can be deleted.")
        if getattr(config, "CLOUD_MODE", False):
            from . import cloud, vectorstore
            domain = get_domain(key)
            for row in cloud.documents(key):
                cloud.delete_document(domain, row["file_hash"])
            vectorstore.reset_collection(domain.collection)
            # Keep the name while any other embedding namespace still has documents.
            if cloud.rows("qa_documents", domain="eq." + key):
                raise ValueError("This base also has documents in another embedding configuration. Remove those before deleting its name.")
            cloud.remove("qa_domains", key="eq." + key)
            DOMAINS.pop(key, None)
            return
        domain = _custom_domain(key, custom[key])
        pdf_root = config.PDF_DIR.resolve()
        target = domain.pdf_dir.resolve()
        if target.parent != pdf_root or target.name != key or domain.pdf_dir.is_symlink():
            raise ValueError("Knowledge base document path is unsafe.")
        # Keep the saved name on failure so cleanup can be retried.
        ingest.reset_domain(key)
        if target.exists():
            shutil.rmtree(target)
        del custom[key]
        _save_custom(custom)
        DOMAINS.pop(key, None)


def system_prompt(domain: Domain) -> str:
    return f"{domain.persona}\n\n{COMMON_RULES}"
