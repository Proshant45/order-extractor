import os, sys, tempfile
from pathlib import Path
_d = tempfile.mkdtemp(prefix="oe_test_")
os.environ["DATA_DIR"] = _d
os.environ["DB_PATH"] = str(Path(_d) / "t.db")
os.environ["TMP_DIR"] = str(Path(_d) / "tmp")
if os.getenv("TEST_USE_LLM") != "1":
    os.environ.pop("GEMINI_API_KEY", None); os.environ.pop("OPENAI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
