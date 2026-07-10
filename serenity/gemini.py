"""
serenity/gemini.py
call_gemini()（原 server.py 259-303 行）
"""
import json
import urllib.error
import urllib.request

from .keypool import _key_manager


def call_gemini(model_name: str, contents: list, system_instruction: str,
                temperature: float = 0.3, response_mime_type: str = None,
                task_class: str = "interactive") -> dict:
    """Unified Gemini API call with KeyManager 429 failover routing."""
    if not _key_manager.has_any_key():
        raise ValueError("尚未設定 Gemini API Key，無法呼叫 AI 服務。")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    req_payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {"temperature": temperature},
    }
    if response_mime_type:
        req_payload["generationConfig"]["responseMimeType"] = response_mime_type

    tried: set = set()
    while True:
        key_entry = _key_manager.pick_key(task_class, exclude=tried)
        req = urllib.request.Request(
            url,
            data=json.dumps(req_payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key_entry["key"],
            },
        )
        _key_manager.record_call(key_entry)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                _key_manager.mark_429(key_entry)
                tried.add(key_entry["label"])
                continue  # retry with next key
            if exc.code == 503:
                _key_manager.mark_503(key_entry)
                tried.add(key_entry["label"])
                try:
                    # Still try remaining keys; if all exhausted, ValueError is raised
                    _key_manager.pick_key(task_class, exclude=tried)
                    continue  # retry with next key
                except ValueError:
                    raise exc  # all keys cooling, re-raise original 503
            raise
