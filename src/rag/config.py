"""All settings in one place. Override any value through the .env file."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")

# ---- folders -------------------------------------------------------------- #
PDF_DIR = BASE_DIR / "pdfs"
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"          # extraction + LLM cache (saves time and money)
REGISTRY_FILE = DATA_DIR / "registry.json"  # which PDFs are already vectorised
QDRANT_PATH = DATA_DIR / "qdrant_db"    # local Qdrant storage (no server needed)

# ---- LLM (DeepSeek, OpenAI-compatible API) -------------------------------- #
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# ---- embeddings ----------------------------------------------------------- #
# multilingual-e5-small understands English and Roman Urdu/Urdu reasonably well.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
NEAR_DUPLICATE_THRESHOLD = float(os.getenv("NEAR_DUPLICATE_THRESHOLD", "0.97"))

# ---- Qdrant --------------------------------------------------------------- #
# Leave QDRANT_URL empty to use the embedded local database in data/qdrant_db.
# For a Qdrant server / Docker / Qdrant Cloud set QDRANT_URL (and QDRANT_API_KEY).
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None

# ---- chunking / retrieval ------------------------------------------------- #
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1000"))
CHUNK_OVERLAP_CHARS = int(os.getenv("CHUNK_OVERLAP_CHARS", "150"))
TOP_K = int(os.getenv("TOP_K", "5"))

# ---- extraction / OCR ----------------------------------------------------- #
MIN_IMAGE_PIXELS = int(os.getenv("MIN_IMAGE_PIXELS", "120"))   # skip tiny icons/logos
OCR_LANG = os.getenv("OCR_LANG", "eng")
OCR_PSM = int(os.getenv("OCR_PSM", "6"))   # 6 = best for tables/infographics inside images


def tesseract_cmd() -> str | None:
    """Find the Tesseract executable (env var, PATH, or the default Windows path)."""
    explicit = os.getenv("TESSERACT_CMD", "").strip()
    if explicit:
        return explicit
    found = shutil.which("tesseract")
    if found:
        return found
    windows_default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    return windows_default if Path(windows_default).exists() else None


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
