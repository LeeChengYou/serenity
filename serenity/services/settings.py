"""
serenity/services/settings.py
階段二：設定 API 的業務邏輯（GET/POST /api/settings, POST /api/settings/test）
安全規則：完整 key 永不出現在任何回應、log、錯誤訊息。
"""
import json
import urllib.error
import urllib.request

from ..config import (
    SERENITY_HOME,
    _VALID_KEYS,
    get_setting,
    load_config,
    save_config,
)
from ..keypool import _key_manager

# key 設定名稱與 slot 號碼的對應
_KEY_FIELDS = [
    "gemini_api_key",
    "gemini_api_key_2",
    "gemini_api_key_3",
    "gemini_api_key_4",
]


def _mask_key(key: str) -> str:
    """遮罩：前 4 字元 + '…' + 後 4 字元。key 不足 8 字元回 '****'。"""
    if not key or len(key) < 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def build_settings_response() -> dict:
    """
    建立 GET /api/settings 回應 dict。
    任何欄位均不含完整 key。
    """
    keys_info = []
    any_set = False
    for i, field in enumerate(_KEY_FIELDS, start=1):
        val = get_setting(field)
        is_set = bool(val)
        if is_set:
            any_set = True
        keys_info.append({
            "slot": i,
            "set": is_set,
            "masked": _mask_key(val) if is_set else None,
        })

    models = {
        "gemini_model":           get_setting("gemini_model"),
        "gemini_translate_model": get_setting("gemini_translate_model"),
        "gemini_memory_model":    get_setting("gemini_memory_model"),
    }

    return {
        "has_key": any_set,
        "keys": keys_info,
        "models": models,
        "config_path": str(SERENITY_HOME / "config.json"),
    }


def handle_post_settings(payload: dict) -> dict:
    """
    POST /api/settings 處理。
    驗證欄位 → save_config → KeyManager.reload() → 回傳 GET 結構。
    未知欄位拋 ValueError（handler 會回 400）。
    """
    unknown = [k for k in payload if k not in _VALID_KEYS]
    if unknown:
        raise ValueError(f"未知設定欄位：{', '.join(unknown)}")

    save_config(payload)
    _key_manager.reload()
    return build_settings_response()


def handle_test_key(payload: dict) -> dict:
    """
    POST /api/settings/test 處理。
    key 為空 → 立即回 ok=false，不打網路。
    否則打 generativelanguage.googleapis.com 驗證（timeout 10s）。
    key 不落地、不落 log。
    """
    key = (payload.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "empty key"}

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()   # consume body
            return {"ok": True}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception:
        return {"ok": False, "error": "連線失敗"}
