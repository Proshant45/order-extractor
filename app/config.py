import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "orders.db"))
TMP_DIR = Path(os.getenv("TMP_DIR", DATA_DIR / "tmp"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
CHUNK = 1024 * 1024
MAX_PAGES = int(os.getenv("MAX_PAGES", "40"))
RENDER_DPI = int(os.getenv("RENDER_DPI", "150"))
MAX_IMAGE_PX = int(os.getenv("MAX_IMAGE_PX", "2000"))
EXTRACT_TIMEOUT_S = int(os.getenv("EXTRACT_TIMEOUT_S", "300"))
LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "90"))
# zip guards
ZIP_MAX_ENTRIES = int(os.getenv("ZIP_MAX_ENTRIES", "200"))
ZIP_MAX_TOTAL_BYTES = int(os.getenv("ZIP_MAX_TOTAL_MB", "200")) * 1024 * 1024
ZIP_MAX_ENTRY_BYTES = MAX_UPLOAD_BYTES
ZIP_MAX_RATIO = float(os.getenv("ZIP_MAX_RATIO", "100"))
ZIP_MAX_DEPTH = 1

def llm_provider():
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return None

for d in (DATA_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)
