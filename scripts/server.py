#!/usr/bin/env python3
import argparse
import json
import sqlite3
import os
import re
import urllib.request
import time
import threading
import subprocess
import sys
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# Technical indicators (stdlib only, no pandas/numpy)
try:
    from indicators import compute_all as _compute_indicators
except ImportError:
    # If server is run from a different cwd, try the scripts/ folder path
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "indicators",
        Path(__file__).resolve().parent / "indicators.py",
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _compute_indicators = _mod.compute_all

# Signal rules engine (SPEC F-06 / F-07)
try:
    from signals import evaluate_signal as _evaluate_signal
except ImportError:
    import importlib.util as _ilu2
    _spec2 = _ilu2.spec_from_file_location(
        "signals",
        Path(__file__).resolve().parent / "signals.py",
    )
    _mod2 = _ilu2.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)
    _evaluate_signal = _mod2.evaluate_signal

# Quantitative X-corpus scorer (from the serenity-stock-scorer skill).  Used as
# a fallback score for symbols that have no AI-generated bottleneck scorecard,
# so every symbol with mentions still gets a signal score.
try:
    import importlib.util as _ilu3
    _spec3 = _ilu3.spec_from_file_location(
        "score_serenity_stock",
        Path(__file__).resolve().parents[1]
        / "skills" / "serenity-stock-scorer" / "scripts" / "score_serenity_stock.py",
    )
    _mod3 = _ilu3.module_from_spec(_spec3)
    _spec3.loader.exec_module(_mod3)
    _quant_score = _mod3.score_symbol
except Exception:
    _quant_score = None

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"
STATIC_DIR = ROOT / "dashboard"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


_server_start_time = time.time()
_schema_initialized = False

def call_gemini(model_name: str, contents: list, system_instruction: str,
                temperature: float = 0.3, response_mime_type: str = None) -> dict:
    """Unified Gemini API call helper. Passes key via HTTP header x-goog-api-key."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in environment.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    req_payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {"temperature": temperature}
    }
    if response_mime_type:
        req_payload["generationConfig"]["responseMimeType"] = response_mime_type
        
    req = urllib.request.Request(
        url,
        data=json.dumps(req_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        }
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))

def db():
    global _schema_initialized
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    if not _schema_initialized:
        _init_schema(con)
        _schema_initialized = True
    return con

def _init_schema(con):
    con.executescript("""
        create table if not exists user_memories (
            id integer primary key autoincrement,
            category text not null,
            symbol text,
            content text not null,
            weight real default 1.0,
            updated_at text not null,
            unique(category, symbol, content)
        );
        create table if not exists scorecards (
            symbol text primary key,
            company text,
            market text,
            final_score real,
            verdict text,
            raw_factor_points real,
            penalty_points real,
            factors_json text,
            penalties_json text,
            evidence_json text,
            kill_switches_json text,
            updated_at text
        );
        create index if not exists idx_user_memories_weight on user_memories(weight desc);
        create index if not exists idx_scorecards_symbol on scorecards(symbol);
    """)
    # Idempotent: scorecard_history table (SPEC F-03)
    con.execute("""
        create table if not exists scorecard_history (
            id          integer primary key autoincrement,
            symbol      text not null,
            final_score real not null,
            verdict     text,
            factors_json text,
            penalties_json text,
            model_used  text,
            created_at  text default (datetime('now'))
        )
    """)
    # R-5: signal_history — daily snapshots for hit-rate tracking (idempotent)
    con.execute("""
        create table if not exists signal_history (
            symbol      text not null,
            date        text not null,
            signal      text,
            score       real,
            score_source text,
            close       real,
            rsi         real,
            atr14       real,
            primary key (symbol, date)
        )
    """)
    # R-3: dossier cache — avoids re-billing Gemini on every view (idempotent)
    con.execute("""
        create table if not exists dossiers (
            symbol      text primary key,
            dossier_json text not null,
            created_at  text not null
        )
    """)
    try:
        con.execute(
            "create index if not exists idx_scorecard_history_symbol "
            "on scorecard_history (symbol, created_at)"
        )
    except Exception:
        pass
    for idx_name, tbl_name, col in [
        ("idx_mentions_symbol", "mentions", "symbol"),
        ("idx_prices_symbol_date", "prices", "symbol, date"),
        ("idx_tweets_created", "tweets", "created_at")
    ]:
        try:
            con.execute(f"create index if not exists {idx_name} on {tbl_name}({col})")
        except Exception:
            pass
    con.commit()


def one(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else {}


class Handler(SimpleHTTPRequestHandler):
    MAX_PAYLOAD = 2 * 1024 * 1024  # 2MB payload limit

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            try:
                payload = self.route_api(parsed.path, parse_qs(parsed.query))
                self.send_json(payload)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                safe_msg = str(exc)
                if "key=" in safe_msg.lower() or "api_key" in safe_msg.lower() or "goog-api-key" in safe_msg.lower():
                    safe_msg = "Internal API request error (credentials hidden for security)"
                self.send_json({"error": safe_msg}, status=500)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > self.MAX_PAYLOAD:
                self.send_json({"error": f"Payload too large (max {self.MAX_PAYLOAD} bytes)"}, status=413)
                return
            post_data = self.rfile.read(content_length).decode("utf-8")
            try:
                payload = {}
                if content_length > 0 and post_data.strip():
                    payload = json.loads(post_data)
                response_payload = self.route_post_api(parsed.path, payload)
                self.send_json(response_payload)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                safe_msg = str(exc)
                if "key=" in safe_msg.lower() or "api_key" in safe_msg.lower() or "goog-api-key" in safe_msg.lower():
                    safe_msg = "Internal API request error (credentials hidden for security)"
                self.send_json({"error": safe_msg}, status=500)
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
        try:
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
            if path.startswith("/api/signal/history/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                rows = [dict(r) for r in con.execute(
                    "select symbol, date, signal, score, score_source, close, rsi, atr14 "
                    "from signal_history where symbol=? order by date asc",
                    (symbol,),
                )]
                return {"symbol": symbol, "history": rows}
            if path.startswith("/api/dossier/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                refresh = (query.get("refresh") or ["0"])[0] == "1"
                return dossier_payload(con, symbol, refresh=refresh)
            if path.startswith("/api/signal/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                return signal_payload(con, symbol)
            if path.startswith("/api/scorecard/history/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                rows = [dict(r) for r in con.execute(
                    "select id, symbol, final_score, verdict, created_at "
                    "from scorecard_history where symbol=? order by created_at asc",
                    (symbol,),
                )]
                return {"symbol": symbol, "history": rows}
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
            if path == "/api/memory":
                try:
                    rows = [dict(r) for r in con.execute("select * from user_memories order by weight desc").fetchall()]
                    return {"memories": rows}
                except Exception as e:
                    return {"error": str(e)}
        finally:
            con.close()
        return {"error": "unknown api"}

    def route_post_api(self, path, payload):
        if path == "/api/chat":
            return handle_chat_api(payload)
        if path == "/api/memory/clear":
            con = db()
            try:
                con.execute("delete from user_memories")
                con.commit()
                return {"success": True}
            except Exception as e:
                return {"error": str(e)}
            finally:
                con.close()
        if path.startswith("/api/scorecard/generate/"):
            symbol = unquote(path.rsplit("/", 1)[-1]).upper().strip()
            try:
                con = db()
                try:
                    tweets = [r[0] for r in con.execute("select text from mentions where symbol=? order by mentioned_at desc limit 20", (symbol,)).fetchall()]
                finally:
                    con.close()
                
                tweets_text = "\n".join([f"- {t}" for t in tweets]) if tweets else "本機資料庫無相關貼文，請以您的知識庫分析該公司。"
                
                system_prompt = (
                    f"你是一個資深晶片與科技半導體供應鏈研究專家。你的任務是分析 {symbol} 這家公司，遵循 serenity-skill 的卡點/瓶頸評級準則，產生一份定性的「供應鏈瓶頸記分卡」。\n"
                    "請嚴格依據事實或您的專業產業知識進行評估，絕不捏造子虛烏有的事實。\n"
                    "必須返回 JSON 格式，包含以下欄位：\n"
                    "{\n"
                    "  \"company\": \"公司官方名稱\",\n"
                    "  \"market\": \"公司掛牌市場 (例如 US Stock / Taiwan Stock)\",\n"
                    "  \"factors\": {\n"
                    "    \"demand_inflection\": 0-5 評級,\n"
                    "    \"architecture_coupling\": 0-5 評級,\n"
                    "    \"chokepoint_severity\": 0-5 評級,\n"
                    "    \"supplier_concentration\": 0-5 評級,\n"
                    "    \"expansion_difficulty\": 0-5 評級,\n"
                    "    \"evidence_quality\": 0-5 評級,\n"
                    "    \"valuation_disconnect\": 0-5 評級,\n"
                    "    \"catalyst_timing\": 0-5 評級\n"
                    "  },\n"
                    "  \"penalties\": {\n"
                    "    \"dilution_financing\": 0-5 評級,\n"
                    "    \"governance\": 0-5 評級,\n"
                    "    \"geopolitics\": 0-5 評級,\n"
                    "    \"liquidity\": 0-5 評級,\n"
                    "    \"hype_risk\": 0-5 評級,\n"
                    "    \"accounting_quality\": 0-5 評級,\n"
                    "    \"cyclicality\": 0-5 評級,\n"
                    "    \"alternative_design_risk\": 0-5 評級\n"
                    "  },\n"
                    "  \"evidence\": [\n"
                    "    {\"claim\": \"事實證據陳述一\", \"source\": \"事實來源\", \"strength\": \"strong/medium/weak之一\"}\n"
                    "  ],\n"
                    "  \"what_could_weaken_view\": [\n"
                    "    \"可能削弱此瓶頸看法的因素一\",\n"
                    "    \"可能削弱此瓶頸看法的因素二\"\n"
                    "  ]\n"
                    "}\n"
                    "注意：請用台灣繁體中文寫所有內容。"
                )
                
                model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
                res_data = call_gemini(
                    model_name=model_name,
                    contents=[{"role": "user", "parts": [{"text": f"請對個股 {symbol} 進行定性供應鏈瓶頸分析。本機資料庫相關貼文如下：\n{tweets_text}"}]}],
                    system_instruction=system_prompt,
                    temperature=0.3,
                    response_mime_type="application/json"
                )
                
                reply_text = res_data['candidates'][0]['content']['parts'][0]['text']
                card_data = json.loads(reply_text)
                
                WEIGHTS = {
                    "demand_inflection": 15,
                    "architecture_coupling": 10,
                    "chokepoint_severity": 15,
                    "supplier_concentration": 12,
                    "expansion_difficulty": 12,
                    "evidence_quality": 15,
                    "valuation_disconnect": 11,
                    "catalyst_timing": 10,
                }
                PENALTY_MULTIPLIER = 2.0
                
                factors = card_data.get("factors", {})
                penalties = card_data.get("penalties", {})
                
                factor_details = {}
                total = 0.0
                for key, weight in WEIGHTS.items():
                    rating = float(factors.get(key, 0))
                    rating = max(0.0, min(5.0, rating))
                    points = rating / 5.0 * weight
                    factor_details[key] = {"rating": rating, "weight": weight, "points": round(points, 2)}
                    total += points
                    
                penalty_details = {}
                penalty_total = 0.0
                for key, val in penalties.items():
                    rating = float(val)
                    rating = max(0.0, min(5.0, rating))
                    points = rating * PENALTY_MULTIPLIER
                    penalty_details[key] = {"rating": rating, "points": round(points, 2)}
                    penalty_total += points
                    
                final_score = max(0.0, min(100.0, total - penalty_total))
                
                if final_score >= 85:
                    verdict = "Top research priority"
                elif final_score >= 70:
                    verdict = "High research priority"
                elif final_score >= 55:
                    verdict = "Worth tracking"
                else:
                    verdict = "Early lead or low priority"
                    
                now_str = datetime.now().isoformat()
                model_name_used = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
                con = db()
                try:
                    # Task D (SPEC F-03): archive existing scorecard to history
                    # BEFORE overwriting, so the timeline is always append-only.
                    existing = con.execute(
                        "select final_score, verdict, factors_json, penalties_json "
                        "from scorecards where symbol=?", (symbol,)
                    ).fetchone()
                    if existing:
                        con.execute(
                            "insert into scorecard_history "
                            "(symbol, final_score, verdict, factors_json, penalties_json, model_used, created_at) "
                            "values (?, ?, ?, ?, ?, ?, ?)",
                            (
                                symbol,
                                existing[0],
                                existing[1],
                                existing[2],
                                existing[3],
                                model_name_used,
                                now_str,
                            ),
                        )

                    con.execute("""
                        insert into scorecards (
                            symbol, company, market, final_score, verdict, 
                            raw_factor_points, penalty_points, 
                            factors_json, penalties_json, evidence_json, kill_switches_json, 
                            updated_at
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        on conflict(symbol) do update set
                            company=excluded.company,
                            market=excluded.market,
                            final_score=excluded.final_score,
                            verdict=excluded.verdict,
                            raw_factor_points=excluded.raw_factor_points,
                            penalty_points=excluded.penalty_points,
                            factors_json=excluded.factors_json,
                            penalties_json=excluded.penalties_json,
                            evidence_json=excluded.evidence_json,
                            kill_switches_json=excluded.kill_switches_json,
                            updated_at=excluded.updated_at
                    """, (
                        symbol,
                        card_data.get("company", symbol),
                        card_data.get("market", "-"),
                        round(final_score, 2),
                        verdict,
                        round(total, 2),
                        round(penalty_total, 2),
                        json.dumps(factor_details, ensure_ascii=False),
                        json.dumps(penalty_details, ensure_ascii=False),
                        json.dumps(card_data.get("evidence", []), ensure_ascii=False),
                        json.dumps(card_data.get("what_could_weaken_view", []), ensure_ascii=False),
                        now_str
                    ))
                    con.commit()
                finally:
                    con.close()
                
                return {"success": True, "final_score": round(final_score, 2)}
            except Exception as e:
                import traceback
                traceback.print_exc()
                safe_msg = str(e)
                if "key=" in safe_msg.lower() or "api_key" in safe_msg.lower() or "goog-api-key" in safe_msg.lower():
                    safe_msg = "Internal API request error (credentials hidden for security)"
                return {"error": safe_msg}
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
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            model_name = payload.get("model") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            
            contents = []
            for msg in messages:
                role = 'user' if msg.get('role') == 'user' else 'model'
                contents.append({"role": role, "parts": [{"text": msg.get('content', '')}]})
                
            start_time = time.time()
            res_data = call_gemini(
                model_name=model_name,
                contents=contents,
                system_instruction=system_instruction,
                temperature=0.3
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
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
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
    
    model_name = os.environ.get("GEMINI_MEMORY_MODEL", "gemini-2.0-flash-lite")
    
    try:
        res_data = call_gemini(
            model_name=model_name,
            contents=[{"role": "user", "parts": [{"text": f"請從以下對話中提煉長期記憶：\n{history_text}"}]}],
            system_instruction=system_prompt,
            temperature=0.2,
            response_mime_type="application/json"
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
    # Fetch full OHLCV — older columns (open/high/low) may be NULL for legacy rows
    bars = [dict(r) for r in con.execute(
        "select date, open, high, low, close, volume from prices where symbol=? order by date",
        (symbol,),
    )]

    # Build the legacy `prices` list (date, close, volume) for backward compat
    prices = [{"date": b["date"], "close": b["close"], "volume": b["volume"]} for b in bars]

    # Compute technical indicators.  Returns null fields when data is insufficient.
    try:
        indicators = _compute_indicators(bars)
    except Exception as exc:
        indicators = {"error": str(exc)}

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
    return {
        "symbol": symbol,
        "prices": prices,
        "bars": bars,
        "indicators": indicators,
        "mentions": mentions,
        "neighbors": neighbors,
    }


def signal_payload(con, symbol: str) -> dict:
    """
    Build the /api/signal/<SYM> response (SPEC F-06 / F-07).

    Pulls real OHLCV bars and scorecard score from the database, computes
    indicators via indicators.compute_all, then delegates to
    signals.evaluate_signal for the rules engine.  Never fabricates prices
    or returns.
    """
    # Fetch real OHLCV bars, oldest-first
    bars = [dict(r) for r in con.execute(
        "select date, open, high, low, close, volume "
        "from prices where symbol=? order by date",
        (symbol,),
    )]

    if not bars:
        return {
            "symbol": symbol,
            "signal": "NEUTRAL",
            "conditions": [],
            "entry_zone": None,
            "stop_loss": None,
            "risk_per_share": None,
            "target": None,
            "rr_ratio": None,
            "atr14": None,
            "score": None,
            "insufficient_data": True,
        }

    # Latest close from real data
    latest_close = None
    for b in reversed(bars):
        c = b.get("close")
        if c is not None:
            try:
                latest_close = float(c)
                break
            except (TypeError, ValueError):
                pass

    if latest_close is None:
        return {
            "symbol": symbol,
            "signal": "NEUTRAL",
            "conditions": [],
            "entry_zone": None,
            "stop_loss": None,
            "risk_per_share": None,
            "target": None,
            "rr_ratio": None,
            "atr14": None,
            "score": None,
            "insufficient_data": True,
        }

    # Compute indicators from real bars
    try:
        indicators = _compute_indicators(bars)
    except Exception as exc:
        indicators = {}

    # Fetch current and previous scorecard scores (real, not synthesised)
    score = None
    prev_score = None
    score_source = None
    sc_row = con.execute(
        "select final_score from scorecards where symbol=?", (symbol,)
    ).fetchone()
    if sc_row and sc_row[0] is not None:
        score = sc_row[0]
        score_source = "scorecard"
    elif _quant_score is not None:
        # Fallback: quantitative X-corpus score so symbols without an AI
        # scorecard still get a signal score.  Computed from real mentions.
        try:
            q = _quant_score(DB_PATH, symbol)
            if q and q.get("score") is not None:
                score = q["score"]
                score_source = "quant"
        except Exception:
            pass

    # Previous score = the most recently archived scorecard.  The current
    # score lives in `scorecards`; every prior version is appended to
    # `scorecard_history` before being overwritten, so the newest history
    # row is the immediately-previous score.
    hist_row = con.execute(
        "select final_score from scorecard_history where symbol=? "
        "order by created_at desc limit 1",
        (symbol,),
    ).fetchone()
    if hist_row:
        prev_score = hist_row[0]

    # Real StockTwits crowd sentiment from news_sentiment (recent tagged msgs)
    sentiment = None
    sent_rows = con.execute(
        "select sentiment from news_sentiment where symbol=? "
        "order by published_at desc limit 100",
        (symbol,),
    ).fetchall()
    if sent_rows:
        bull = sum(1 for (s,) in sent_rows if s == "Bullish")
        bear = sum(1 for (s,) in sent_rows if s == "Bearish")
        tagged = bull + bear
        sentiment = {
            "bull": bull,
            "bear": bear,
            "total": tagged,
            "ratio": (bull / tagged) if tagged else None,
        }

    result = _evaluate_signal(
        latest_close=latest_close,
        indicators=indicators,
        score=score,
        bars=bars,
        prev_score=prev_score,
        rr_ratio=2.0,
        sentiment=sentiment,
    )
    result["symbol"] = symbol
    result["score_source"] = score_source
    return result


_RELIABILITY_NOTE = (
    "Serenity signals have NOT been validated out-of-sample. All historical "
    "scores and signal states are computed on in-sample data; no temporal "
    "hold-out test has been conducted. Do not treat any output as investment "
    "advice or as evidence of predictive capability."
)


def dossier_payload(con, symbol: str, refresh: bool = False) -> dict:
    """
    R-3: Build (or return cached) the /api/dossier/<SYM> response.

    Assembles all real evidence from SQLite, generates a Gemini manager_view
    narrative from ONLY that data, and caches the result in the `dossiers`
    table.  Pass refresh=True to bypass the cache and regenerate.

    Graceful degradation: if the Gemini call fails (no key, network error,
    parse error), manager_view is set to null and the full data dossier is
    still returned.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")

    # --- 1. Check cache (unless refresh requested) ---
    if not refresh:
        cached = con.execute(
            "select dossier_json from dossiers where symbol=?", (symbol,)
        ).fetchone()
        if cached:
            try:
                return json.loads(cached[0])
            except Exception:
                pass

    # --- 2. Fetch real OHLCV bars ---
    bars = [dict(r) for r in con.execute(
        "select date, open, high, low, close, volume "
        "from prices where symbol=? order by date",
        (symbol,),
    )]

    latest_close = None
    for b in reversed(bars):
        c = b.get("close")
        if c is not None:
            try:
                latest_close = float(c)
                break
            except (TypeError, ValueError):
                pass

    # --- 3. Compute indicators ---
    indicators = {}
    if bars:
        try:
            indicators = _compute_indicators(bars)
        except Exception:
            pass

    ema20_series = indicators.get("ema20", [])
    ema50_series = indicators.get("ema50", [])
    rsi_series = indicators.get("rsi14", [])
    atr14 = indicators.get("atr14")

    def _lat(series):
        for v in reversed(series):
            if v is not None:
                return v
        return None

    ema20 = _lat(ema20_series)
    ema50 = _lat(ema50_series)
    rsi = _lat(rsi_series)

    # --- 4. Quant score + components ---
    score = None
    score_source = None
    quant_components = {}
    sc_row = con.execute(
        "select final_score from scorecards where symbol=?", (symbol,)
    ).fetchone()
    if sc_row and sc_row[0] is not None:
        score = sc_row[0]
        score_source = "scorecard"

    if _quant_score is not None:
        try:
            q = _quant_score(DB_PATH, symbol)
            if q:
                quant_components = q.get("components", {})
                if score is None and q.get("score") is not None:
                    score = q["score"]
                    score_source = "quant"
        except Exception:
            pass

    quant_section = {
        "score": score,
        "source": score_source,
        "components": quant_components,
    }

    # --- 5. Technicals section ---
    trend = None
    if latest_close is not None and ema50 is not None:
        trend = "above EMA50" if latest_close > ema50 else "below EMA50"
    elif latest_close is not None and ema20 is not None:
        trend = "above EMA20" if latest_close > ema20 else "below EMA20"

    atr_pct = None
    if atr14 is not None and latest_close is not None and latest_close > 0:
        atr_pct = round(atr14 / latest_close * 100, 2)

    technicals_section = {
        "latest_close": latest_close,
        "trend": trend,
        "ema20": round(ema20, 4) if ema20 is not None else None,
        "ema50": round(ema50, 4) if ema50 is not None else None,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "atr_pct": atr_pct,
    }

    # --- 6. Signal section (reuse signal_payload internals) ---
    sig_data = signal_payload(con, symbol)
    signal_section = {
        "state": sig_data.get("signal", "NEUTRAL"),
        "key_conditions": [
            c for c in (sig_data.get("conditions") or []) if c.get("met")
        ],
        "entry_zone": sig_data.get("entry_zone"),
        "stop_loss": sig_data.get("stop_loss"),
        "target": sig_data.get("target"),
        "ema20_ref": sig_data.get("ema20_ref"),
    }

    # --- 7. Sentiment section ---
    sent_rows = con.execute(
        "select sentiment from news_sentiment where symbol=? "
        "order by published_at desc limit 100",
        (symbol,),
    ).fetchall()
    sentiment_section = None
    if sent_rows:
        bull = sum(1 for (s,) in sent_rows if s == "Bullish")
        bear = sum(1 for (s,) in sent_rows if s == "Bearish")
        tagged = bull + bear
        sentiment_section = {
            "stocktwits_bull_ratio": round(bull / tagged, 3) if tagged else None,
            "bull": bull,
            "bear": bear,
            "sample": tagged,
        }

    # --- 8. Scorecard summary ---
    sc_full = con.execute(
        "select symbol, final_score, verdict, factors_json from scorecards where symbol=?",
        (symbol,),
    ).fetchone()
    scorecard_section = None
    if sc_full:
        top_factors = []
        try:
            fd = json.loads(sc_full["factors_json"] or "{}")
            top_factors = sorted(
                [{"name": k, "points": v.get("points", 0)} for k, v in fd.items()],
                key=lambda x: x["points"],
                reverse=True,
            )[:3]
        except Exception:
            pass
        scorecard_section = {
            "final_score": sc_full["final_score"],
            "verdict": sc_full["verdict"],
            "top_factors": top_factors,
        }

    # --- 9. Evidence: top 3 tweets by engagement ---
    evidence_rows = con.execute(
        """select m.text, t.url, t.favorite_count, t.reply_count,
                  t.retweet_count, m.mentioned_at
           from mentions m join tweets t on t.tweet_id=m.tweet_id
           where m.symbol=?
           order by (coalesce(t.favorite_count,0)
                     + 2*coalesce(t.retweet_count,0)
                     + coalesce(t.reply_count,0)) desc
           limit 3""",
        (symbol,),
    ).fetchall()
    evidence_section = [
        {
            "text": (r["text"] or "")[:400],
            "url": r["url"],
            "date": (r["mentioned_at"] or "")[:10],
            "engagement": (
                (r["favorite_count"] or 0)
                + 2 * (r["retweet_count"] or 0)
                + (r["reply_count"] or 0)
            ),
        }
        for r in evidence_rows
    ]

    # --- 10. Build Gemini prompt and call for manager_view ---
    manager_view = None
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            prompt_data = {
                "symbol": symbol,
                "as_of": as_of,
                "latest_close": latest_close,
                "quant_score": score,
                "score_source": score_source,
                "quant_components": quant_components,
                "technicals": technicals_section,
                "signal_state": signal_section["state"],
                "key_conditions_met": [c["label"] for c in signal_section["key_conditions"]],
                "entry_zone": signal_section["entry_zone"],
                "stop_loss": signal_section["stop_loss"],
                "target": signal_section["target"],
                "sentiment": sentiment_section,
                "scorecard": scorecard_section,
                "top_evidence_snippets": [e["text"][:200] for e in evidence_section],
            }

            system_instruction = (
                "You are a senior quantitative investment analyst generating a structured manager-view dossier. "
                "CRITICAL RULES:\n"
                "1. You MUST use ONLY the data provided in the user's JSON payload. Do NOT introduce any external facts, "
                "market knowledge, or opinions not present in the payload.\n"
                "2. If the data is insufficient, say so explicitly in the thesis field.\n"
                "3. All numbers you cite must come verbatim from the payload.\n"
                "4. Return ONLY valid JSON with exactly these fields: "
                "thesis (string), bull_case (string), bear_case (string), "
                "conviction (one of: LOW, MEDIUM, HIGH), "
                "recommendation (one of: AVOID, WATCH, ACCUMULATE, HOLD, REDUCE), "
                "position_guidance (string — ATR-based, anchored to latest close from the payload).\n"
                "5. Write in English. Be concise. Do not invent facts."
            )

            user_text = (
                f"Generate a manager-view dossier for ${symbol} using ONLY the following real data:\n\n"
                + json.dumps(prompt_data, ensure_ascii=False, indent=2)
            )

            model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            res_data = call_gemini(
                model_name=model_name,
                contents=[{"role": "user", "parts": [{"text": user_text}]}],
                system_instruction=system_instruction,
                temperature=0.25,
                response_mime_type="application/json",
            )
            reply_text = res_data["candidates"][0]["content"]["parts"][0]["text"]
            mv = json.loads(reply_text)
            # Defensive: ensure required keys exist
            manager_view = {
                "thesis": str(mv.get("thesis") or ""),
                "bull_case": str(mv.get("bull_case") or ""),
                "bear_case": str(mv.get("bear_case") or ""),
                "conviction": str(mv.get("conviction") or "LOW"),
                "recommendation": str(mv.get("recommendation") or "WATCH"),
                "position_guidance": str(mv.get("position_guidance") or ""),
            }
        except Exception as exc:
            print(f"[Dossier] Gemini call failed for {symbol}: {exc}")
            manager_view = None

    # --- 11. Assemble full dossier ---
    dossier = {
        "symbol": symbol,
        "as_of": as_of,
        "quant": quant_section,
        "technicals": technicals_section,
        "signal": signal_section,
        "sentiment": sentiment_section,
        "scorecard": scorecard_section,
        "evidence": evidence_section,
        "manager_view": manager_view,
        "reliability_note": _RELIABILITY_NOTE,
    }

    # --- 12. Cache in dossiers table ---
    try:
        con.execute(
            """insert into dossiers (symbol, dossier_json, created_at)
               values (?, ?, ?)
               on conflict(symbol) do update set
                   dossier_json=excluded.dossier_json,
                   created_at=excluded.created_at""",
            (symbol, json.dumps(dossier, ensure_ascii=False), datetime.now().isoformat()),
        )
        con.commit()
    except Exception as exc:
        print(f"[Dossier] Cache write failed for {symbol}: {exc}")

    return dossier


def snapshot_signals():
    """
    R-5: Upsert today's signal row for every symbol that has price data.

    Idempotent — running twice on the same day overwrites the row with
    identical data, leaving the table consistent.  Reuses signal_payload
    so all computation is DRY.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    con = db()
    try:
        symbols = [
            r[0] for r in con.execute(
                "select distinct symbol from prices"
            ).fetchall()
        ]
        inserted = 0
        for sym in symbols:
            try:
                sp = signal_payload(con, sym)
                rsi_val = None
                if sp.get("conditions"):
                    for cond in sp["conditions"]:
                        if "RSI" in cond.get("label", "") and "RSI: " in cond.get("detail", ""):
                            try:
                                rsi_val = float(cond["detail"].split("RSI: ")[1].split()[0])
                            except Exception:
                                pass
                            break

                latest_close = None
                bars = con.execute(
                    "select close from prices where symbol=? order by date desc limit 1",
                    (sym,),
                ).fetchone()
                if bars:
                    latest_close = bars[0]

                con.execute(
                    """insert into signal_history
                           (symbol, date, signal, score, score_source, close, rsi, atr14)
                       values (?, ?, ?, ?, ?, ?, ?, ?)
                       on conflict(symbol, date) do update set
                           signal=excluded.signal,
                           score=excluded.score,
                           score_source=excluded.score_source,
                           close=excluded.close,
                           rsi=excluded.rsi,
                           atr14=excluded.atr14""",
                    (
                        sym,
                        today,
                        sp.get("signal"),
                        sp.get("score"),
                        sp.get("score_source"),
                        latest_close,
                        rsi_val,
                        sp.get("atr14"),
                    ),
                )
                inserted += 1
            except Exception as exc:
                print(f"[Snapshot] {sym} failed: {exc}")
        con.commit()
        print(f"[Snapshot] signal_history upserted {inserted} rows for {today}.")
        return inserted
    finally:
        con.close()


def run_background_ingest():
    # Wait 10 seconds after server starts
    time.sleep(10)
    while True:
        try:
            print("[Scheduler] Starting automatic incremental price and X fetch...")
            interpreter = sys.executable or "python"
            script_path = ROOT / "scripts" / "ingest.py"
            res = subprocess.run([interpreter, str(script_path), "prices"], capture_output=True, text=True)
            if res.returncode == 0:
                print("[Scheduler] Automatic incremental price update completed successfully.")
            else:
                print(f"[Scheduler] Price update returned code {res.returncode}. Stderr: {res.stderr.strip()}")

            # R-5: snapshot today's signals for hit-rate tracking
            try:
                snapshot_signals()
            except Exception as snap_exc:
                print(f"[Scheduler] snapshot_signals warning: {snap_exc}")

            # Apply memory decay daily
            decay_memories()
        except Exception as e:
            print(f"[Scheduler] Background ingest warning: {e}")

        # Run every 12 hours
        time.sleep(12 * 3600)


def main():
    ap = argparse.ArgumentParser(description="Serve the Serenity dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument(
        "--snapshot-once",
        action="store_true",
        help="R-5: Run signal_history snapshot for all priced symbols and exit (no server start).",
    )
    args = ap.parse_args()

    # R-5 CLI hook: run snapshot and exit immediately (no server start)
    if args.snapshot_once:
        count = snapshot_signals()
        print(f"[snapshot-once] Done — {count} rows upserted.")
        return

    # Start background scheduler thread
    t = threading.Thread(target=run_background_ingest, daemon=True)
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
