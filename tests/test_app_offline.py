"""Offline UI test with Streamlit's AppTest (fake embedder + stub LLM):  python tests/test_app_offline.py"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fakes import FakeEmbedder  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from rag import config, embed, llm, vectorstore  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.DATA_DIR, config.CACHE_DIR = tmp, tmp / "cache"
config.REGISTRY_FILE, config.QDRANT_PATH = tmp / "registry.json", tmp / "qdrant_db"
embed.set_embedder(FakeEmbedder())
calls = []


def fake_chat_json(system, user, max_tokens=3000, retries=3):
    calls.append(user)
    if "Rewrite the user's last message" in system:
        return {"query": "standalone query"}
    return {"answer": "STUB ANSWER from the excerpts.", "sources": [1]}


llm.chat_json = fake_chat_json
config.DEEPSEEK_API_KEY = "test"


def main() -> None:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    assert not at.exception, at.exception
    assert at.sidebar.selectbox[0].options == ["🎧 Customer Support", "⚖️ Legal Assistant"]
    print("dropdown OK:", at.sidebar.selectbox[0].options)

    at.chat_input[0].set_value("How long does express delivery take?").run()
    assert not at.exception, at.exception
    texts = [m.markdown[0].value for m in at.chat_message if m.markdown]
    assert any("STUB ANSWER" in t for t in texts), texts
    print("customer support chat OK")

    at.sidebar.selectbox[0].select("⚖️ Legal Assistant").run()
    assert not at.exception, at.exception
    at.chat_input[0].set_value("What is the liability cap?").run()
    texts = [m.markdown[0].value for m in at.chat_message if m.markdown]
    assert any("STUB ANSWER" in t for t in texts), texts
    print("legal assistant chat OK")

    n_before = len(calls)
    at.chat_input[0].set_value("And what are the exceptions?").run()  # follow-up -> rewrite step runs
    assert len(calls) > n_before
    print("follow-up question OK")
    vectorstore.close_client()
    print("ALL APP CHECKS PASSED")


if __name__ == "__main__":
    main()
