"""
serenity/config.py
ROOT, DB_PATH, STATIC_DIR, .env 載入（原 server.py 70-89 行）
"""
import os
from pathlib import Path

# ROOT must point to repo root; this file lives at serenity/config.py,
# so parents[1] is the repo root (same as original server.py's parents[1]).
ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"
STATIC_DIR = ROOT / "dashboard"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    # stdlib fallback so the Gemini key still loads without python-dotenv:
    # parse simple KEY=VALUE lines, skipping comments and blank lines.
    _env_file = ROOT / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
            if _k and _k not in os.environ:
                os.environ[_k] = _v
