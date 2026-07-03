#!/usr/bin/env python3
import argparse
import json
import sqlite3
import os
import re
import urllib.request
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"
STATIC_DIR = ROOT / "dashboard"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def one(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else {}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            try:
                payload = self.route_api(parsed.path, parse_qs(parsed.query))
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length).decode("utf-8")
            try:
                payload = json.loads(post_data)
                response_payload = self.route_post_api(parsed.path, payload)
                self.send_json(response_payload)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return
        self.send_response(404)
        self.end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route_api(self, path, query):
        if path == "/api/config":
            return {
                "has_key": bool(os.environ.get("GEMINI_API_KEY")),
                "default_model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            }
        if path == "/api/monitor":
            log_path = ROOT / "data" / "chat_monitor.json"
            logs = []
            if log_path.exists():
                try:
                    logs = json.loads(log_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return {"logs": logs}
        if not DB_PATH.exists():
            return {"error": f"database not found: {DB_PATH}"}
        con = db()
        if path == "/api/summary":
            return summary(con)
        if path == "/api/feed":
            limit = int((query.get("limit") or [80])[0])
            return {"items": [dict(r) for r in con.execute(
                """select m.symbol, m.mentioned_at, m.text, t.url, t.favorite_count, t.reply_count, t.source
                       from mentions m join tweets t on t.tweet_id=m.tweet_id
                       order by m.mentioned_at desc limit ?""", (limit,))]}
        if path.startswith("/api/symbol/"):
            symbol = unquote(path.rsplit("/", 1)[-1]).upper()
            return symbol_payload(con, symbol)
        if path.startswith("/api/scorecard/"):
            symbol = unquote(path.rsplit("/", 1)[-1]).upper()
            row = con.execute("select * from scorecards where symbol=?", (symbol,)).fetchone()
            if row:
                res = dict(row)
                try:
                    res["factor_details"] = json.loads(res["factors_json"])
                    res["penalty_details"] = json.loads(res["penalties_json"])
                    res["evidence"] = json.loads(res["evidence_json"])
                    res["kill_switches"] = json.loads(res["kill_switches_json"])
                except Exception:
                    pass
                return res
            return {}
        return {"error": "unknown api"}

    def route_post_api(self, path, payload):
        if path == "/api/chat":
            return handle_chat_api(payload)
        return {"error": "unknown api"}


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
            con = sqlite3.connect(DB_PATH)
            con.row_factory = sqlite3.Row
            
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
            zh_words = re.findall(r'[\u4e00-\u9fa5]{2,}', user_message)
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
                
    # 2. Read skill instructions
    skill_path = ROOT / "skills" / "serenity-skill" / "SKILL.md"
    skill_content = ""
    if skill_path.exists():
        skill_content = skill_path.read_text(encoding="utf-8", errors="replace")
        
    system_instruction = (
        "你是一位專業的 AI 投資研究夥伴 (Serenity)。你遵循 'serenity-skill' 的供應鏈瓶頸分析架構來回答使用者問題。\n"
        "請使用與使用者相同的語言回答（如果使用者使用繁體中文，請用繁體中文回答；如果使用者使用簡體中文，請用簡體中文回答）。\n\n"
        f"這裡是你必須嚴格遵守的 serenity-skill 研究準則與工作流：\n{skill_content}\n\n"
        f"這裡是使用者提問相關的本機 SQLite 資料庫資料快照：\n{db_context}\n"
        "注意：回答時請保持專業、直接、理性、客觀且具有洞察力，避免空泛的投資建議。引用資料時，請直接使用上述提供的資料庫快照與事實。"
    )
    
    # 3. Call LLM or fallback
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            model_name = payload.get("model") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            
            contents = []
            for msg in messages:
                role = 'user' if msg.get('role') == 'user' else 'model'
                contents.append({"role": role, "parts": [{"text": msg.get('content', '')}]})
                
            req_payload = {
                "contents": contents,
                "systemInstruction": {
                    "parts": [{"text": system_instruction}]
                },
                "generationConfig": {
                    "temperature": 0.3
                }
            }
            
            start_time = time.time()
            req = urllib.request.Request(
                url,
                data=json.dumps(req_payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
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
                
                return {"response": reply}
        except Exception as e:
            return {
                "response": (
                    f"❌ **[AI 呼叫失敗]**：{e}\n\n"
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


def summary(con):
    stats = one(con, """
        select (select count(*) from tweets) tweets,
               (select count(*) from mentions) mentions,
               (select count(distinct symbol) from mentions) symbols,
               (select max(mentioned_at) from mentions) latest_mention,
               (select count(distinct symbol) from prices) priced_symbols
    """)
    symbols = []
    for r in con.execute("""
        select m.symbol, count(*) mention_count, max(m.mentioned_at) latest_mention,
               min(m.mentioned_at) first_mention,
               (select close from prices p where p.symbol=m.symbol order by date desc limit 1) last_close,
               (select date from prices p where p.symbol=m.symbol order by date desc limit 1) last_price_date,
               (select count(*) from prices p where p.symbol=m.symbol) price_bars
        from mentions m
        group by m.symbol
        order by mention_count desc, latest_mention desc
    """):
        d = dict(r)
        d["has_prices"] = bool(d.pop("price_bars"))
        symbols.append(d)
    return {"stats": stats, "symbols": symbols}


def symbol_payload(con, symbol):
    prices = [dict(r) for r in con.execute(
        "select date, close, volume from prices where symbol=? order by date", (symbol,)
    )]
    mentions = [dict(r) for r in con.execute(
        """select m.symbol, m.mentioned_at, m.text, t.url, t.favorite_count, t.reply_count, t.retweet_count, t.source
               from mentions m join tweets t on t.tweet_id=m.tweet_id
               where m.symbol=? order by m.mentioned_at""", (symbol,)
    )]
    neighbors = [dict(r) for r in con.execute(
        """select m2.symbol, count(*) count
               from mentions m1 join mentions m2 on m1.tweet_id=m2.tweet_id and m1.symbol<>m2.symbol
               where m1.symbol=? group by m2.symbol order by count desc, m2.symbol limit 20""", (symbol,)
    )]
    return {"symbol": symbol, "prices": prices, "mentions": mentions, "neighbors": neighbors}


def main():
    ap = argparse.ArgumentParser(description="Serve the Serenity dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
