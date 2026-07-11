"""
serenity/services/translate.py
handle_translate_api
（原 server.py 1082-1235 行）
"""
import hashlib
import json
import os
from datetime import datetime

from ..config import DB_PATH, get_setting
from ..db import db
from ..gemini import call_gemini
from ..keypool import _key_manager


def handle_translate_api(payload: dict) -> dict:
    """
    POST /api/translate
    Request:  {"texts": [...]}   max 20 items
    Response: {"translations": [...], "cached": [...], "error": null | "zh-TW msg"}

    Cache-first: cached texts are NEVER re-sent to Gemini.
    Single Gemini call for all uncached texts (task_class="translate").
    """
    texts = payload.get("texts")
    if not texts or not isinstance(texts, list):
        return {
            "translations": [],
            "cached": [],
            "error": "請提供 texts 欄位（字串陣列），且不可為空。",
        }
    if len(texts) > 20:
        return {
            "translations": [],
            "cached": [],
            "error": f"每次最多翻譯 20 條文本（收到 {len(texts)} 條）。",
        }

    n = len(texts)
    results: list = [None] * n
    cached_flags: list = [False] * n
    uncached_indices: list = []

    # --- 1. Cache lookup ---
    con = None
    try:
        if DB_PATH.exists():
            con = db()
            for i, text in enumerate(texts):
                if not isinstance(text, str) or not text.strip():
                    continue
                h = hashlib.sha256(text.encode("utf-8")).hexdigest()
                row = con.execute(
                    "select translated_text from translations where src_hash=?", (h,)
                ).fetchone()
                if row:
                    results[i] = row[0]
                    cached_flags[i] = True
                else:
                    uncached_indices.append(i)
        else:
            uncached_indices = [i for i, t in enumerate(texts) if isinstance(t, str) and t.strip()]
    except Exception as exc:
        print(f"[translate] cache lookup error: {exc}")
        uncached_indices = [i for i, t in enumerate(texts) if isinstance(t, str) and t.strip()]
    finally:
        if con:
            con.close()

    # All cached → return immediately
    if not uncached_indices:
        return {"translations": results, "cached": cached_flags, "error": None}

    # --- 2. No key → return cached results with error for uncached slots ---
    if not _key_manager.has_any_key():
        return {
            "translations": results,
            "cached": cached_flags,
            "error": "尚未設定 Gemini API Key，無法執行翻譯。",
        }

    # --- 3. Single Gemini call for all uncached texts ---
    uncached_texts = [texts[i] for i in uncached_indices]
    translate_model = get_setting("gemini_translate_model")

    system_prompt = (
        "你是一個專業的財經新聞翻譯員。請將使用者提供的英文文本翻譯成台灣繁體中文。\n"
        "嚴格規則：\n"
        "1. 股票代號（如 NVDA、TSM、AAPL、AMD）、數字、百分比、金額保留原文不翻譯。\n"
        "2. 公司名稱使用台灣通用中文譯名（如 Apple=蘋果、Nvidia=輝達），無通用譯名則保留英文。\n"
        "3. 只回傳一個 JSON 陣列，陣列長度與輸入相同，不加任何說明、前綴、後綴或其他文字。\n"
        "4. 若某條文本無法翻譯，該位置填入原文字串。"
    )
    user_text = (
        f"請翻譯以下 {len(uncached_texts)} 條文本。"
        f"回傳 JSON 陣列，長度必須恰好為 {len(uncached_texts)}：\n"
        + json.dumps(uncached_texts, ensure_ascii=False)
    )

    error_msg = None
    try:
        res_data = call_gemini(
            model_name=translate_model,
            contents=[{"role": "user", "parts": [{"text": user_text}]}],
            system_instruction=system_prompt,
            temperature=0.1,
            response_mime_type="application/json",
            task_class="translate",
        )
        reply_text = res_data["candidates"][0]["content"]["parts"][0]["text"]
        translations = json.loads(reply_text)

        if not isinstance(translations, list):
            error_msg = "翻譯 API 回傳格式錯誤（非 JSON 陣列），部分結果未翻譯。"
            translations = [None] * len(uncached_texts)
        elif len(translations) != len(uncached_texts):
            error_msg = (
                f"翻譯 API 回傳長度不符（預期 {len(uncached_texts)}，"
                f"實際 {len(translations)}），部分結果未翻譯。"
            )
            # Pad or truncate to match
            while len(translations) < len(uncached_texts):
                translations.append(None)
            translations = translations[: len(uncached_texts)]

        # Fill results and cache successful translations
        now_str = datetime.now().isoformat()
        con2 = None
        try:
            if DB_PATH.exists():
                con2 = db()
        except Exception:
            con2 = None

        for idx_in_batch, orig_idx in enumerate(uncached_indices):
            trans = translations[idx_in_batch]
            if trans and isinstance(trans, str):
                results[orig_idx] = trans
                # Cache
                if con2 is not None:
                    try:
                        src_text = texts[orig_idx]
                        h = hashlib.sha256(src_text.encode("utf-8")).hexdigest()
                        con2.execute(
                            """insert into translations
                               (src_hash, src_text, translated_text, model, created_at)
                               values (?, ?, ?, ?, ?)
                               on conflict(src_hash) do nothing""",
                            (h, src_text, trans, translate_model, now_str),
                        )
                    except Exception as cache_exc:
                        print(f"[translate] cache write error: {cache_exc}")

        if con2 is not None:
            try:
                con2.commit()
            except Exception:
                pass
            finally:
                con2.close()

    except Exception as exc:
        safe = str(exc)
        # Mask any accidental key leakage
        if any(k in safe.lower() for k in ("key=", "api_key", "goog-api-key")):
            safe = "AI 服務請求錯誤（憑證資訊已遮蔽）"
        error_msg = f"翻譯暫時不可用：{safe[:120]}"

    return {"translations": results, "cached": cached_flags, "error": error_msg}
