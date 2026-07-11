"""
serenity/services/chat.py
handle_chat_api, log_chat_transaction, consolidate_memory_in_background,
extract_memory_task, decay_memories
（原 server.py 847-1075 + 1238-1307 行）
"""
import json
import os
import re
import threading
import time
from datetime import datetime

from ..config import DB_PATH, ROOT, get_setting
from ..db import db
from ..gemini import call_gemini
from ..keypool import _key_manager


def log_chat_transaction(tx):
    log_path = ROOT / "data" / "chat_monitor.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logs = []
    if log_path.exists():
        try:
            logs = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    logs.insert(0, tx)
    logs = logs[:100]
    try:
        log_path.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def handle_chat_api(payload):
    messages = payload.get("messages", [])
    if not messages:
        return {"error": "no messages provided"}

    user_message = messages[-1].get("content", "")

    # 1. Look for symbols and topics in the message
    mentioned_symbols = []
    db_context = ""
    con = None
    if DB_PATH.exists():
        try:
            con = db()

            # Get list of all known symbols in DB
            db_symbols = [r[0] for r in con.execute("select distinct symbol from mentions").fetchall()]

            # Extract explicit symbols (e.g. $TSM, NVDA)
            words = re.findall(r'[A-Za-z0-9.]+', user_message.upper())
            for word in words:
                word_clean = word.lstrip('$')
                if word_clean in db_symbols and word_clean not in mentioned_symbols:
                    mentioned_symbols.append(word_clean)

            # Extract theme keywords (Semantic RAG)
            topic_keywords = []
            CHOKEPOINT_THEMES = {
                "先進封裝": ["packaging", "cowos", "封装", "先進封裝", "封裝", "hbm"],
                "液冷散熱": ["cooling", "liquid", "散热", "液冷", "散熱", "水冷"],
                "機器人減速器": ["robot", "reducer", "harmonic", "gear", "減速器", "諧波", "機器人", "齒輪"],
                "光通訊矽光子": ["optical", "photonics", "cpo", "光模組", "光模塊", "矽光子", "光電"],
                "半導體材料": ["photoresist", "silicon", "substrate", "materials", "光阻劑", "矽晶圓", "化學品", "材料", "靶材"]
            }
            user_lower = user_message.lower()
            for theme, kws in CHOKEPOINT_THEMES.items():
                if any(kw in user_lower for kw in kws):
                    topic_keywords.extend(kws)

            # Also extract other potential English terms or Chinese words of length >= 2
            zh_words = re.findall(r'[一-龥]{2,}', user_message)
            en_words = [w.lower() for w in re.findall(r'[A-Za-z]{4,}', user_message)]
            stopwords = {"with", "that", "this", "from", "about", "what", "have", "some", "your", "them", "analyst", "stock", "view"}
            for w in en_words:
                if w not in stopwords and w not in topic_keywords:
                    topic_keywords.append(w)
            for w in zh_words:
                if w not in topic_keywords:
                    topic_keywords.append(w)

            matched_tweets = []
            if topic_keywords:
                conditions = " OR ".join(["text LIKE ?" for _ in topic_keywords])
                params = [f"%{k}%" for k in topic_keywords]
                sql = f"""
                    select m.symbol, m.text, m.mentioned_at
                    from mentions m
                    where {conditions}
                    order by m.mentioned_at desc limit 8
                """
                matched_rows = con.execute(sql, params).fetchall()
                for r in matched_rows:
                    matched_tweets.append(dict(r))

            # If topic-relevant tweets are matched, inject them as context and auto-associate their symbols
            if matched_tweets:
                db_context += "\n這裡是與您詢問的主題/關鍵字相關的 X 社群貼文觀點（由系統自動檢索注入）：\n"
                for idx, t in enumerate(matched_tweets, 1):
                    db_context += f"  [{idx}] [${t['symbol']}] [{t['mentioned_at']}] {t['text'].strip()}\n"

                # Auto-associate symbols from matched tweets (limit total symbols to 5 to avoid context blowup)
                for t in matched_tweets:
                    sym = t['symbol'].upper()
                    if sym not in mentioned_symbols and len(mentioned_symbols) < 5:
                        mentioned_symbols.append(sym)

            # Pull metrics and prices for all identified symbols (explicit + auto-associated)
            if mentioned_symbols:
                db_context += "\n這裡是相關個股在本機資料庫中的最新資料與價格（由系統自動注入）：\n"
                for sym in mentioned_symbols:
                    count = con.execute("select count(*) from mentions where symbol=?", (sym,)).fetchone()[0]
                    tweets = [dict(r) for r in con.execute(
                        "select text, mentioned_at from mentions where symbol=? order by mentioned_at desc limit 2", (sym,)
                    ).fetchall()]
                    prices = [dict(r) for r in con.execute(
                        "select date, close from prices where symbol=? order by date desc limit 5", (sym,)
                    ).fetchall()]

                    db_context += f"- 股票代號 {sym}:\n"
                    db_context += f"  * X 社群提及次數: {count} 次\n"
                    if tweets:
                        db_context += f"  * 最新 X 貼文觀點:\n"
                        for t in tweets:
                            db_context += f"    - [{t['mentioned_at']}] {t['text'].strip()}\n"
                    if prices:
                        db_context += f"  * 最新歷史股價收盤價 (由近到遠):\n"
                        for p in prices:
                            db_context += f"    - [{p['date']}] {p['close']}\n"
        except Exception as e:
            db_context = f"\n[資料庫查詢錯誤: {e}]\n"
        finally:
            if con:
                con.close()

    # 1.1 Read Long-Term memories (Persistent Memory across model switches)
    memories_context = ""
    if DB_PATH.exists():
        try:
            con = db()
            rows = con.execute("select category, symbol, content from user_memories where weight > 0 order by weight desc").fetchall()
            if rows:
                memories_context = "\n【本機儲存的使用者長期記憶與歷史偏好快照】（請遵循這些偏好來回答，但絕不捏造事實）：\n"
                for r in rows:
                    sym_part = f" (關於個股 ${r['symbol']})" if r['symbol'] else ""
                    memories_context += f"  - [{r['category']}] {r['content']}{sym_part}\n"
            con.close()
        except Exception as e:
            print(f"Error loading memories: {e}")

    # 2. Read skill instructions
    skill_path = ROOT / "skills" / "serenity-skill" / "SKILL.md"
    skill_content = ""
    if skill_path.exists():
        skill_content = skill_path.read_text(encoding="utf-8", errors="replace")

    system_instruction = (
        "你是一位專業的 AI 投資研究夥伴 (Serenity)。你遵循 'serenity-skill' 的供應鏈瓶鏈分析架構來回答使用者問題。\n"
        "請使用與使用者相同的語言回答（如果使用者使用繁體中文，請用繁體中文回答；如果使用者使用簡體中文，請用簡體中文回答）。\n\n"
        "【嚴格禁止幻覺與虛構】\n"
        "1. 僅基於本機資料庫提供的事實數據與推文內容回答問題。\n"
        "2. 絕對不能捏造任何股票代碼、提及次數、價格、日期或社群觀點。\n"
        "3. 歷史長期記憶中提及的偏好僅用於引導回答風格與關聯討論，不作為捏造事實的依據。\n"
        "4. 如果資料庫中沒有相關的價格或推文數據，必須直接承認，絕不編造。\n\n"
        f"這裡是你必須嚴格遵守的 serenity-skill 研究準則與工作流：\n{skill_content}\n\n"
        f"這裡是使用者提問相關的本機 SQLite 資料庫資料快照：\n{db_context}\n"
        f"{memories_context}\n"
        "注意：回答時請保持專業、直接、理性、客觀且具有洞察力，避免空泛的投資建議。引用資料時，請直接使用上述提供的資料庫快照與事實。"
    )

    # 3. Call LLM or fallback
    if _key_manager.has_any_key():
        try:
            model_name = payload.get("model") or get_setting("gemini_model")

            contents = []
            for msg in messages:
                role = 'user' if msg.get('role') == 'user' else 'model'
                contents.append({"role": role, "parts": [{"text": msg.get('content', '')}]})

            start_time = time.time()
            res_data = call_gemini(
                model_name=model_name,
                contents=contents,
                system_instruction=system_instruction,
                temperature=0.3,
                task_class="interactive",
            )

            reply = res_data['candidates'][0]['content']['parts'][0]['text']

            usage = res_data.get("usageMetadata", {})
            prompt_tokens = usage.get("promptTokenCount", 0)
            completion_tokens = usage.get("candidatesTokenCount", 0)
            total_tokens = usage.get("totalTokenCount", 0)
            time_taken = round((time.time() - start_time) * 1000)

            log_chat_transaction({
                "timestamp": datetime.now().isoformat(),
                "model": model_name,
                "prompt": user_message,
                "response": reply,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "latency_ms": time_taken,
                "system_instruction_len": len(system_instruction)
            })

            # Trigger memory consolidation task in background
            consolidate_memory_in_background(messages, reply)

            return {"response": reply}
        except Exception as e:
            import traceback
            traceback.print_exc()
            safe_msg = str(e)
            if "key=" in safe_msg.lower() or "api_key" in safe_msg.lower() or "goog-api-key" in safe_msg.lower():
                safe_msg = "Internal API request error (credentials hidden for security)"
            return {
                "response": (
                    f"❌ **[AI 呼叫失敗]**：{safe_msg}\n\n"
                    "**[本機資料庫查詢結果]**：\n"
                    f"偵測到您詢問了個股：{', '.join(mentioned_symbols) if mentioned_symbols else '無股票代號'}\n"
                    f"{db_context if db_context else '未在您的問題中偵測到資料庫已有的個股名稱。'}"
                )
            }
    else:
        return {
            "response": (
                "⚠️ **[系統提示] 目前尚未設定 Gemini API Key。**\n\n"
                "要啟用真實 AI 對話，請在專案目錄下建立 `.env` 檔案並填入 `GEMINI_API_KEY=your_key`，然後重啟伺服器。\n\n"
                "**[本機資料庫查詢結果]**：\n"
                f"偵測到您詢問了個股：{', '.join(mentioned_symbols) if mentioned_symbols else '無'}\n"
                f"{db_context if db_context else '未在您的問題中偵測到資料庫已有的個股名稱。'}\n\n"
                "*(提示：設定 API 金鑰後，模型即可結合上述資料庫資料與 Serenity 瓶頸記分卡進行深入的瓶頸分析。)*"
            )
        }


def consolidate_memory_in_background(messages, ai_reply):
    t = threading.Thread(target=extract_memory_task, args=(messages, ai_reply), daemon=True)
    t.start()


def extract_memory_task(messages, ai_reply):
    if not _key_manager.has_any_key():
        return

    history_text = ""
    for m in messages[-4:]:
        role = "使用者" if m.get("role") == "user" else "AI"
        history_text += f"{role}: {m.get('content')}\n"
    history_text += f"AI: {ai_reply}\n"

    system_prompt = (
        "你是一個長期記憶提煉器。分析以下對話，提取出使用者的偏好（preference）、最關注的產業或個股（interest）、以及雙方達成的關鍵研究結論（conclusion）。\n"
        "排除日常問候。回傳格式必須是 JSON Array，且只包含 category, symbol, content 三個欄位。如果沒有提取出任何記憶，回傳空陣列 []。\n"
        "範例輸出：\n"
        "[\n"
        "  {\"category\": \"interest\", \"symbol\": \"TSM\", \"content\": \"使用者高度關注 TSM 的 CoWoS 先進封裝產能缺口。\"},\n"
        "  {\"category\": \"preference\", \"symbol\": \"\", \"content\": \"使用者偏好高強度證據來源，並對估值過高持謹慎態度。\"}\n"
        "]"
    )

    model_name = get_setting("gemini_memory_model")

    try:
        res_data = call_gemini(
            model_name=model_name,
            contents=[{"role": "user", "parts": [{"text": f"請從以下對話中提煉長期記憶：\n{history_text}"}]}],
            system_instruction=system_prompt,
            temperature=0.2,
            response_mime_type="application/json",
            task_class="memory",
        )
        reply_text = res_data['candidates'][0]['content']['parts'][0]['text']
        memories = json.loads(reply_text)
        if isinstance(memories, list):
            con = db()
            try:
                now = datetime.now().isoformat()
                for item in memories:
                    cat = item.get("category", "interest")
                    sym = (item.get("symbol") or "").upper().strip()
                    content = item.get("content", "").strip()
                    if content:
                        con.execute(
                            """insert into user_memories(category, symbol, content, weight, updated_at)
                               values (?, ?, ?, 1.0, ?)
                               on conflict(category, symbol, content) do update set weight=1.0, updated_at=excluded.updated_at""",
                            (cat, sym, content, now)
                        )
                con.commit()
                print(f"[Memory] Consolidation successful. Extracted {len(memories)} memory items.")
            finally:
                con.close()
    except Exception as e:
        print(f"[Memory] Failed to consolidate memory: {e}")


def decay_memories():
    con = db()
    try:
        con.execute("""
            update user_memories
            set weight = weight - (julianday('now') - julianday(updated_at)) * 0.1
        """)
        con.execute("delete from user_memories where weight <= 0")
        con.commit()
        print("[Scheduler] Memory time-decay applied successfully.")
    except Exception as e:
        print(f"[Scheduler] Failed to decay memories: {e}")
    finally:
        con.close()
