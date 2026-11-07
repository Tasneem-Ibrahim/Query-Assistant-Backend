"""Streamlit chat app.   Run:  streamlit run app.py

Pick an assistant in the sidebar dropdown (Customer Support / Legal Assistant). Each assistant has
its own PDFs and its own Qdrant collection. PDFs are processed ONCE; after that every question only
searches the vector database.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import streamlit as st  # noqa: E402

from rag import answer, config, ingest  # noqa: E402
from rag.domains import DOMAINS  # noqa: E402

st.set_page_config(page_title="PDF Assistants", page_icon="📄", layout="wide")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def run_ingest(domain, pdfs, force: bool = False) -> None:
    """Process PDFs with a live progress box. Already-processed PDFs are skipped automatically."""
    with st.status(f"Processing PDF for {domain.label} ...", expanded=True) as box:
        try:
            for pdf in pdfs:
                report = ingest.ingest_pdf(domain, pdf, force=force, progress=lambda m: box.write(m))
                box.write(f"**{report['name']}** -> {report['status']} ({report['chunks']} chunks)")
            box.update(label="PDF is ready. You can ask questions now.", state="complete", expanded=False)
        except Exception as exc:  # show the problem instead of a stack trace
            box.update(label="Processing failed", state="error")
            st.error(str(exc))


def show_sources(sources: list[dict]) -> None:
    if not sources:
        return
    with st.expander(f"Sources ({len(sources)})"):
        for hit in sources:
            m = hit["metadata"]
            title = f"**{m['source']} - page {m['page']}**" + (f" - {m['heading']}" if m.get("heading") else "")
            st.markdown(title)
            st.caption(hit["text"][:500] + ("..." if len(hit["text"]) > 500 else ""))


# --------------------------------------------------------------------------- #
# sidebar: assistant dropdown + document status
# --------------------------------------------------------------------------- #
labels = {f"{d.icon} {d.label}": key for key, d in DOMAINS.items()}
with st.sidebar:
    st.title("📄 PDF Assistants")
    choice = st.selectbox("Choose assistant", list(labels))
    domain = DOMAINS[labels[choice]]
    st.divider()

    info = ingest.status(domain.key)

    # first visit: process the bundled PDF automatically (once)
    flag = f"auto_ingested_{domain.key}"
    if info["chunks"] == 0 and domain.default_pdfs() and not st.session_state.get(flag):
        st.session_state[flag] = True
        run_ingest(domain, domain.default_pdfs())
        info = ingest.status(domain.key)

    st.subheader("Knowledge base")
    if info["chunks"]:
        st.success(f"{info['chunks']} chunks ready")
        for f in info["files"].values():
            st.caption(f"📄 {f['name']} - {f['pages']} pages")
    else:
        st.warning("No PDF processed yet.")

    uploaded = st.file_uploader("Add another PDF", type="pdf", key=f"upload_{domain.key}")
    if uploaded and st.button("Process uploaded PDF", use_container_width=True):
        domain.pdf_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", uploaded.name)
        target = domain.pdf_dir / safe
        target.write_bytes(uploaded.getvalue())
        run_ingest(domain, [target])
        st.rerun()

    with st.expander("Advanced"):
        if st.button("Rebuild from scratch", use_container_width=True, help="Deletes this assistant's vectors and re-processes its PDFs"):
            ingest.reset_domain(domain.key)
            run_ingest(domain, domain.default_pdfs(), force=True)
            st.rerun()

    if st.button("🗑️ Clear chat", use_container_width=True):
        st.session_state[f"history_{domain.key}"] = []
        st.rerun()

    if not config.DEEPSEEK_API_KEY:
        st.error("DEEPSEEK_API_KEY is not set. Add it to .env to get answers.")


# --------------------------------------------------------------------------- #
# main: chat
# --------------------------------------------------------------------------- #
st.header(f"{domain.icon} {domain.label}")
history_key = f"history_{domain.key}"
history: list[dict] = st.session_state.setdefault(history_key, [])

with st.chat_message("assistant"):
    st.write(domain.welcome)

for message in history:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message["role"] == "assistant":
            show_sources(message.get("sources", []))

if not history:
    st.caption("Try one of these:")
    cols = st.columns(2)
    for i, example in enumerate(domain.examples):
        if cols[i % 2].button(example, key=f"ex_{domain.key}_{i}", use_container_width=True):
            st.session_state["pending_question"] = example
            st.rerun()

typed = st.chat_input("Ask a question about the document (English or Roman Urdu)")
question = st.session_state.pop("pending_question", None) or typed

if question:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        if info["chunks"] == 0:
            st.warning("Please process a PDF first.")
        else:
            with st.spinner("Searching the document ..."):
                try:
                    result = answer.ask(domain, question, history=[{"role": m["role"], "content": m["content"]} for m in history])
                except Exception as exc:
                    st.error(str(exc))
                    result = None
            if result:
                st.write(result["answer"])
                show_sources(result["sources"])
                history.append({"role": "user", "content": question})
                history.append({"role": "assistant", "content": result["answer"], "sources": result["sources"]})
