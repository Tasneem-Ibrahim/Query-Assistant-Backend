"""All settings in one place. Override any value through the .env file."""

from __future__ import annotations

import os
import shutil
import hashlib
import tempfile
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
CLOUD_MODE = os.getenv("STORAGE_BACKEND", "supabase" if os.getenv("VERCEL") else "local") == "supabase"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
# The fallback stays on the server; never send either secret to the frontend.
AUTH_COOKIE_SECRET = os.getenv("AUTH_COOKIE_SECRET", "") or SUPABASE_SERVICE_ROLE_KEY
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true" if os.getenv("VERCEL") else "false").lower() == "true"
AUTH_COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "lax").lower()
AUTH_ACCESS_SECONDS = 900
AUTH_SESSION_SECONDS = 7 * 24 * 60 * 60
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "knowledge-pdfs")
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "api" if CLOUD_MODE else "local")
EMBEDDING_API_URL = os.getenv("EMBEDDING_API_URL", "")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_API_MODEL = os.getenv("EMBEDDING_API_MODEL", "")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "0") or "0")
OCR_API_URL = os.getenv("OCR_API_URL", "https://api.mistral.ai/v1/ocr")
OCR_API_KEY = os.getenv("OCR_API_KEY", "")
OCR_API_MODEL = os.getenv("OCR_API_MODEL", "mistral-ocr-latest")
# Local browser hosts must be listed explicitly; localhost and 127.0.0.1 are distinct origins.
CORS_ORIGINS = [value.strip() for value in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if value.strip()]
MAX_UPLOAD_BYTES = 4_000_000  # Leave room for multipart headers below Vercel's request limit.


def embedding_namespace() -> str:
    identity = f"{EMBEDDING_API_URL}|{EMBEDDING_API_MODEL}|{EMBEDDING_DIMENSIONS}"
    return "api_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def configuration_errors() -> list[str]:
    required = {}
    if CLOUD_MODE:
        required.update(SUPABASE_URL=SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SERVICE_ROLE_KEY, QDRANT_URL=QDRANT_URL)
        if EMBEDDING_PROVIDER != "api":
            return ["Cloud mode requires EMBEDDING_PROVIDER=api"]
    if EMBEDDING_PROVIDER == "api":
        required.update(EMBEDDING_API_URL=EMBEDDING_API_URL, EMBEDDING_API_KEY=EMBEDDING_API_KEY, EMBEDDING_API_MODEL=EMBEDDING_API_MODEL, EMBEDDING_DIMENSIONS=EMBEDDING_DIMENSIONS)
    errors = [f"Set {key}" for key, value in required.items() if not value]
    if EMBEDDING_PROVIDER == "api" and EMBEDDING_DIMENSIONS < 0:
        errors.append("EMBEDDING_DIMENSIONS must be positive")
    return errors

# ---- folders -------------------------------------------------------------- #
PDF_DIR = BASE_DIR / "pdfs"
DATA_DIR = Path(tempfile.gettempdir()) / "query-assistant" if CLOUD_MODE else BASE_DIR / "data"
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
