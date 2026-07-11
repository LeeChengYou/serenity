"""
serenity/config.py
ROOT, DB_PATH, STATIC_DIR, .env 載入（原 server.py 70-89 行）
+ 階段二：SERENITY_HOME, load_config, save_config, get_setting
+ 階段三：frozen（PyInstaller onedir）支援
"""
import json
import os
import sys
from pathlib import Path

# ── frozen（PyInstaller onedir）vs 開發模式 ───────────────────────────────────
if getattr(sys, "frozen", False):
    # 打包後：sys._MEIPASS 是 onedir 資源根；使用者資料放 SERENITY_HOME
    ROOT = Path(sys._MEIPASS)
else:
    # 開發模式：此檔在 serenity/config.py，parents[1] = repo root
    ROOT = Path(__file__).resolve().parents[1]

STATIC_DIR = ROOT / "dashboard"

# ── .env 載入（SERENITY_NO_DOTENV=1 時跳過，供測試隔離）──────────────────────
if not os.environ.get("SERENITY_NO_DOTENV"):
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

# ── SERENITY_HOME：使用者設定目錄 ──────────────────────────────────────────────
def _get_serenity_home() -> Path:
    """解析 SERENITY_HOME 路徑（env 優先；否則平台預設）。"""
    if os.environ.get("SERENITY_HOME"):
        p = Path(os.environ["SERENITY_HOME"])
    elif os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        p = Path(local_app) / "Serenity"
    else:
        p = Path.home() / ".serenity"
    p.mkdir(parents=True, exist_ok=True)
    return p


SERENITY_HOME: Path = _get_serenity_home()

# ── DB_PATH：解析順序：SERENITY_DB_PATH env > frozen→SERENITY_HOME > repo data/ > SERENITY_HOME
if os.environ.get("SERENITY_DB_PATH"):
    # 環境變數直接覆寫（與 scorer skill 同名，供測試隔離）
    DB_PATH: Path = Path(os.environ["SERENITY_DB_PATH"])
elif getattr(sys, "frozen", False):
    # 打包執行：資料庫存放在使用者目錄
    DB_PATH = SERENITY_HOME / "serenity.sqlite"
else:
    # 開發模式例外：repo data/ 存在就用 repo 路徑（維持現有排程/測試行為不變）
    _repo_db = ROOT / "data" / "serenity.sqlite"
    if _repo_db.exists():
        DB_PATH = _repo_db
    else:
        # 否則回落到 SERENITY_HOME（使用者可透過 --db 或 CLI 覆蓋）
        DB_PATH = SERENITY_HOME / "serenity.sqlite"

# ── config.json 合法欄位（§2.1 schema 8 個）──────────────────────────────────
_VALID_KEYS = {
    "gemini_api_key",
    "gemini_api_key_2",
    "gemini_api_key_3",
    "gemini_api_key_4",
    "gemini_model",
    "gemini_translate_model",
    "gemini_memory_model",
    "auth_token",
}

_DEFAULTS = {
    "gemini_model": "gemini-2.5-flash",
    "gemini_translate_model": "gemini-2.5-flash-lite",
    "gemini_memory_model": "gemini-2.0-flash-lite",
    "gemini_api_key": "",
    "gemini_api_key_2": "",
    "gemini_api_key_3": "",
    "gemini_api_key_4": "",
    "auth_token": "",
}

# env var mapping（設定名稱 → 環境變數名稱）
_ENV_MAP = {
    "gemini_api_key":   "GEMINI_API_KEY",
    "gemini_api_key_2": "GEMINI_API_KEY_2",
    "gemini_api_key_3": "GEMINI_API_KEY_3",
    "gemini_api_key_4": "GEMINI_API_KEY_4",
    "gemini_model":           "GEMINI_MODEL",
    "gemini_translate_model": "GEMINI_TRANSLATE_MODEL",
    "gemini_memory_model":    "GEMINI_MEMORY_MODEL",
    "auth_token":             "SERENITY_AUTH_TOKEN",
}


def load_config() -> dict:
    """讀取 SERENITY_HOME/config.json，缺欄填預設值。回傳完整 dict。"""
    cfg_path = SERENITY_HOME / "config.json"
    data = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    # 只保留合法欄位
    return {k: data.get(k, _DEFAULTS.get(k, "")) for k in _VALID_KEYS}


def save_config(partial: dict) -> None:
    """部分更新 config.json（只覆蓋傳入欄位）。空字串 = 清除該欄。"""
    # 驗證欄位名合法
    for k in partial:
        if k not in _VALID_KEYS:
            raise ValueError(f"未知設定欄位：{k}")

    cfg_path = SERENITY_HOME / "config.json"
    existing = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    # 合并：空字串=清除（從 config 移除，回落到 env 或預設）
    for k, v in partial.items():
        if v == "":
            existing.pop(k, None)
        else:
            existing[k] = v

    # 只保留合法欄位
    cleaned = {k: v for k, v in existing.items() if k in _VALID_KEYS}
    cfg_path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")


def get_setting(name: str) -> str:
    """
    解析順序：環境變數 > config.json > 預設值。
    name 是 _VALID_KEYS 中的設定名稱。
    """
    # 1. 環境變數
    env_name = _ENV_MAP.get(name)
    if env_name:
        val = os.environ.get(env_name)
        if val:
            return val

    # 2. config.json
    cfg_path = SERENITY_HOME / "config.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            val = data.get(name)
            if val:
                return val
        except Exception:
            pass

    # 3. 預設值
    return _DEFAULTS.get(name, "")
