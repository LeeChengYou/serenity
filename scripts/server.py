#!/usr/bin/env python3
import argparse
import json
import sqlite3
import os
import re
import hashlib
import urllib.error
import urllib.request
import time
import threading
import subprocess
import sys
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------------------
# Benchmark symbols — same constant as ingest.py (R3-2).
# Excluded from all universe queries: signals, snapshots, hit-rate, /api/symbols.
# ---------------------------------------------------------------------------
BENCHMARK_SYMBOLS: set = {"SPY", "SOXX", "QQQ"}

# Technical indicators (stdlib only, no pandas/numpy)
_compute_ema = None  # populated below
try:
    from indicators import compute_all as _compute_indicators
    from indicators import compute_ema as _compute_ema
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
    _compute_ema = _mod.compute_ema

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


_server_start_time = time.time()
_schema_initialized = False

# ---------------------------------------------------------------------------
# R4-1: KeyManager — multi-key Gemini API pool with 429 failover
# ---------------------------------------------------------------------------

class KeyManager:
    """
    Thread-safe Gemini API key pool.

    Task affinity:
      interactive → KEY_1   (chat, dossier)
      batch       → KEY_2   (scorecard generation)
      translate   → KEY_3   (translation)
      memory      → KEY_1   (memory extraction, lite model)
      agent_arena → round-robin across all 4 keys (9 agents spread evenly)
    Overflow: KEY_4 used when affinity key is 429/503-cooling.
    Cooling:  first 429/503 → 60 s; 3rd within 10 min → until next Pacific midnight.
    """
    _AFFINITY = {
        "interactive":  "KEY_1",
        "batch":        "KEY_2",
        "translate":    "KEY_3",
        "memory":       "KEY_1",
        # agent_arena uses round-robin (see _arena_rr_index), not a fixed key
    }
    _OVERFLOW = "KEY_4"
    _ALL_LABELS = ["KEY_1", "KEY_2", "KEY_3", "KEY_4"]
    _arena_rr_index = 0  # class-level round-robin counter for agent_arena
    _ENV_NAMES  = ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"]

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict = {}
        for label, env_name in zip(self._ALL_LABELS, self._ENV_NAMES):
            val = os.environ.get(env_name)
            if val:
                self._entries[label] = {
                    "label":            label,
                    "key":              val,
                    "cooling_until":    None,   # Unix timestamp or None
                    "calls_today":      0,
                    "errors_429_today": 0,
                    "recent_429s":      [],     # timestamps within 10 min
                }

    def has_any_key(self) -> bool:
        return bool(self._entries)

    def _ordered_labels(self, task_class: str) -> list:
        """Preferred label order for a given task class.

        agent_arena: round-robin starting point rotates across all keys so
        9 concurrent agents spread their quota load evenly.
        Others: affinity key first → KEY_4 overflow → remaining.
        """
        if task_class == "agent_arena":
            # Round-robin: each call advances the starting index
            with self._lock:
                idx = KeyManager._arena_rr_index % len(self._ALL_LABELS)
                KeyManager._arena_rr_index += 1
            # Build order starting from round-robin position
            rotated = self._ALL_LABELS[idx:] + self._ALL_LABELS[:idx]
            return [lbl for lbl in rotated if lbl in self._entries]

        affinity = self._AFFINITY.get(task_class, "KEY_1")
        order = [affinity]
        if self._OVERFLOW not in order:
            order.append(self._OVERFLOW)
        for label in self._ALL_LABELS:
            if label not in order:
                order.append(label)
        return [lbl for lbl in order if lbl in self._entries]

    def pick_key(self, task_class: str = "interactive", exclude: set = None) -> dict:
        """Return the best available (non-cooling, non-excluded) key entry."""
        exclude = exclude or set()
        with self._lock:
            now = time.time()
            for label in self._ordered_labels(task_class):
                if label in exclude:
                    continue
                entry = self._entries[label]
                cool = entry["cooling_until"]
                if cool is None or now >= cool:
                    return entry
            raise ValueError("所有 Gemini API Key 目前均在冷卻中，請稍後再試。")

    def _mark_error(self, entry: dict, code: int) -> None:
        """Shared logic for 429/503: record error and set cooling."""
        with self._lock:
            now = time.time()
            entry["errors_429_today"] += 1
            entry["recent_429s"].append(now)
            # Prune older than 10 min
            entry["recent_429s"] = [t for t in entry["recent_429s"] if now - t <= 600]
            if len(entry["recent_429s"]) >= 3:
                cool_ts = self._next_pacific_midnight()
                entry["cooling_until"] = cool_ts
                print(f"[KeyManager] {entry['label']}: 3 {code}s in 10 min → cooling until Pacific midnight")
            else:
                entry["cooling_until"] = now + 60
                print(f"[KeyManager] {entry['label']}: HTTP {code} → cooling 60 s")

    def mark_429(self, entry: dict) -> None:
        """Record a 429 and update cooling state for the given entry."""
        self._mark_error(entry, 429)

    def mark_503(self, entry: dict) -> None:
        """Record a 503 and update cooling state (same policy as 429)."""
        self._mark_error(entry, 503)

    def record_call(self, entry: dict) -> None:
        with self._lock:
            entry["calls_today"] += 1

    def _next_pacific_midnight(self) -> float:
        """Unix timestamp of next midnight in US/Pacific time."""
        from datetime import timedelta
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("US/Pacific")
            now_pac = datetime.now(tz)
            midnight = (now_pac + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            return midnight.timestamp()
        except Exception:
            # Fallback: approximate PDT = UTC-7
            now_utc = datetime.utcnow()
            pac_now = now_utc - timedelta(hours=7)
            midnight = (pac_now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            return time.time() + (midnight - pac_now).total_seconds()

    def status(self) -> list:
        """Return masked status for /api/keypool (last-4 suffix only, never full key)."""
        with self._lock:
            now = time.time()
            result = []
            for label in self._ALL_LABELS:
                if label not in self._entries:
                    continue
                entry = self._entries[label]
                cool = entry["cooling_until"]
                available = cool is None or now >= cool
                cooling_iso = None
                if cool and not available:
                    try:
                        cooling_iso = datetime.fromtimestamp(cool).isoformat()
                    except Exception:
                        pass
                result.append({
                    "label":            label,
                    "suffix":           f"...{entry['key'][-4:]}",
                    "available":        available,
                    "cooling_until":    cooling_iso,
                    "calls_today":      entry["calls_today"],
                    "errors_429_today": entry["errors_429_today"],
                })
            return result


_key_manager = KeyManager()


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
    # R3-5: signal_changes — tracks daily signal transitions (idempotent)
    con.execute("""
        create table if not exists signal_changes (
            symbol      text not null,
            date        text not null,
            prev_signal text,
            new_signal  text,
            primary key (symbol, date)
        )
    """)
    # R3-1: hitrate_cache — caches point-in-time reconstruction results (idempotent)
    con.execute("""
        create table if not exists hitrate_cache (
            cache_key      text primary key,
            max_price_date text not null,
            cache_json     text not null,
            computed_at    text not null
        )
    """)
    # R3-3: analyst_estimates — price targets and EPS estimates (idempotent)
    con.execute("""
        create table if not exists analyst_estimates (
            symbol                   text primary key,
            target_mean              real,
            target_median            real,
            target_high              real,
            target_low               real,
            n_analysts               integer,
            recommendation_key       text,
            recommendation_mean      real,
            eps_estimate_current_q   real,
            eps_estimate_next_q      real,
            eps_estimate_current_y   real,
            up_revisions_30d         integer,
            down_revisions_30d       integer,
            updated_at               text not null
        )
    """)
    try:
        con.execute(
            "create index if not exists idx_scorecard_history_symbol "
            "on scorecard_history (symbol, created_at)"
        )
    except Exception:
        pass
    # R4-2: translation cache (idempotent)
    con.execute("""
        create table if not exists translations (
            src_hash       text primary key,
            src_text       text not null,
            translated_text text not null,
            model          text,
            created_at     text not null
        )
    """)
    # R5-2: expert_views — credible manager holdings from EDGAR 13F etc (idempotent)
    con.execute("""
        create table if not exists expert_views (
            id           integer primary key autoincrement,
            source       text not null,
            author       text,
            title        text,
            text         text not null,
            url          text unique not null,
            published_at text,
            symbols      text,
            credibility  text not null default 'individual',
            fetched_at   text not null
        )
    """)
    con.execute("""
        create index if not exists idx_expert_views_published
            on expert_views(published_at desc)
    """)
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
        # R4-1: key pool status (no DB needed; suffixes only — never full key values)
        if path == "/api/keypool":
            return {
                "keys": _key_manager.status(),
                "as_of": datetime.now().isoformat(),
            }
        if not DB_PATH.exists():
            return {"error": f"database not found: {DB_PATH}"}
        con = db()
        try:
            # --- R3-2 Regime gauge ---
            if path == "/api/regime":
                return regime_payload(con)
            # --- R3-1 Hit-rate ---
            if path == "/api/hitrate":
                return hitrate_payload(con)
            # --- R3-5 Signal changes ---
            if path == "/api/changes":
                days = int((query.get("days") or [7])[0])
                return changes_payload(con, days)
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
            if path.startswith("/api/news/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                return news_payload(con, symbol)
            # --- R3-3 Analyst estimates ---
            if path.startswith("/api/estimates/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                return estimates_payload(con, symbol)
            if path.startswith("/api/fundamentals/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                return fundamentals_payload(con, symbol)
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
            # R5-3: Expert views — all symbols (latest 20)
            if path == "/api/expert-views":
                return expert_views_all_payload(con)
            # R5-3: Expert views — per symbol
            if path.startswith("/api/expert-views/"):
                symbol = unquote(path.rsplit("/", 1)[-1]).upper()
                return expert_views_payload(con, symbol)
            # V6 Arena API routes
            if path == "/api/arena/leaderboard":
                month = (query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
                return arena_leaderboard_payload(con, month)
            if path == "/api/arena/nav":
                month = (query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
                return arena_nav_payload(con, month)
            if path == "/api/arena/trades":
                agent = (query.get("agent") or [""])[0]
                month = (query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
                return arena_trades_payload(con, agent, month)
            if path == "/api/arena/reflections":
                month = (query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
                return arena_reflections_payload(con, month)
        finally:
            con.close()
        return {"error": "unknown api"}

    def route_post_api(self, path, payload):
        if path == "/api/chat":
            return handle_chat_api(payload)
        # R4-2: translation endpoint
        if path == "/api/translate":
            return handle_translate_api(payload)
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
                    response_mime_type="application/json",
                    task_class="batch",
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
    if _key_manager.has_any_key():
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


# ---------------------------------------------------------------------------
# R4-2: Translation API handler
# ---------------------------------------------------------------------------

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
    translate_model = os.environ.get("GEMINI_TRANSLATE_MODEL", "gemini-2.5-flash-lite")

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
    
    model_name = os.environ.get("GEMINI_MEMORY_MODEL", "gemini-2.0-flash-lite")
    
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


def _table_exists(con, table_name: str) -> bool:
    """Return True if the named table exists in the database."""
    row = con.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def news_payload(con, symbol: str) -> dict:
    """
    GET /api/news/<SYM>
    Returns up to 20 symbol-scoped items (newest first) and up to 10 macro items.
    Returns sane empty structure when the news table is absent or empty.
    Contract (REQUIREMENTS_V2.md §三):
      {"symbol", "items":[{title,source,url,published_at,scope,summary},...],
       "macro":[...same shape...], "as_of"}
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    empty = {"symbol": symbol, "items": [], "macro": [], "as_of": as_of}

    if not _table_exists(con, "news"):
        return empty

    def _row_to_item(r):
        return {
            "title": r["title"],
            "source": r["source"],
            "url": r["url"],
            "published_at": r["published_at"],
            "scope": r["scope"],
            "summary": r["summary"],
        }

    try:
        # Symbol-scoped: articles whose symbols JSON contains this symbol
        sym_rows = con.execute(
            """select title, source, url, published_at, scope, summary
               from news
               where scope='symbol'
                 and (symbols like ? or symbols like ? or symbols like ? or symbols like ?)
               order by published_at desc
               limit 20""",
            (
                f'["{symbol}"]',          # exact single-element array
                f'["{symbol}",%',         # first element
                f'%,"{symbol}",%',        # middle element
                f'%,"{symbol}"]',         # last element
            ),
        ).fetchall()
        items = [_row_to_item(r) for r in sym_rows]

        # Macro: any scope='macro' news, newest first, max 10
        macro_rows = con.execute(
            """select title, source, url, published_at, scope, summary
               from news
               where scope='macro'
               order by published_at desc
               limit 10""",
        ).fetchall()
        macro = [_row_to_item(r) for r in macro_rows]

        return {"symbol": symbol, "items": items, "macro": macro, "as_of": as_of}

    except Exception as exc:
        print(f"[news_payload] {symbol}: {exc}")
        return empty


def fundamentals_payload(con, symbol: str) -> dict:
    """
    GET /api/fundamentals/<SYM>
    Returns one row from fundamentals table, all contract fields, nulls where absent.
    Contract (REQUIREMENTS_V2.md §三):
      {"symbol","pe","forward_pe","eps_ttm","revenue_growth_yoy",
       "gross_margin","market_cap","next_earnings_date","updated_at"}
    Returns sane empty structure (all nulls) when table is absent or symbol not found.
    """
    base = {
        "symbol": symbol,
        "pe": None,
        "forward_pe": None,
        "eps_ttm": None,
        "revenue_growth_yoy": None,
        "gross_margin": None,
        "market_cap": None,
        "next_earnings_date": None,
        "updated_at": None,
    }

    if not _table_exists(con, "fundamentals"):
        return base

    try:
        row = con.execute(
            """select symbol, pe, forward_pe, eps_ttm, revenue_growth_yoy,
                      gross_margin, market_cap, next_earnings_date, updated_at
               from fundamentals where symbol=?""",
            (symbol,),
        ).fetchone()
        if row:
            return dict(row)
        return base
    except Exception as exc:
        print(f"[fundamentals_payload] {symbol}: {exc}")
        return base


# ---------------------------------------------------------------------------
# R3-2: Regime gauge helpers
# ---------------------------------------------------------------------------

def _last_non_none(series: list):
    """Return the last non-None value in a series."""
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _benchmark_block(con, symbol: str):
    """EMA200 stance for one benchmark ETF: {"close","ema200","above"} or None."""
    try:
        closes = [r[0] for r in con.execute(
            "select close from prices where symbol=? and close is not null order by date",
            (symbol,),
        )]
        if len(closes) < 200 or _compute_ema is None:
            return None
        ema200 = _last_non_none(_compute_ema(closes, 200))
        if ema200 is None:
            return None
        return {
            "close": round(closes[-1], 4),
            "ema200": round(ema200, 4),
            "above": closes[-1] > ema200,
        }
    except Exception:
        return None


def regime_payload(con) -> dict:
    """
    GET /api/regime  (contract: REQUIREMENTS_V3.md R3-2)

    Regime rule (spec):
      bull:    SPY>EMA200 AND SOXX>EMA200 AND >50% of universe above EMA50
      bear:    SPY<EMA200 OR SOXX<EMA200*0.97
      neutral: everything else
      unknown: benchmark data missing/insufficient

    Returns:
      {"as_of","regime","spy":{close,ema200,above},"soxx":{...},"qqq":{...},
       "universe_above_ema50_pct","note"}
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    base = {
        "as_of": as_of,
        "regime": "unknown",
        "spy": None, "soxx": None, "qqq": None,
        "universe_above_ema50_pct": None,
        "note": "缺乏基準指數資料，無法判斷市場環境。請先執行 `python scripts/ingest.py benchmarks`。",
    }

    try:
        spy  = _benchmark_block(con, "SPY")
        soxx = _benchmark_block(con, "SOXX")
        qqq  = _benchmark_block(con, "QQQ")

        # Universe breadth: % of non-benchmark symbols above their EMA50
        univ_pct = None
        try:
            syms = [r[0] for r in con.execute(
                "select distinct symbol from prices "
                "where symbol not in ('SPY','SOXX','QQQ')"
            )]
            above = total = 0
            for sym in syms:
                closes = [r[0] for r in con.execute(
                    "select close from prices where symbol=? and close is not null order by date",
                    (sym,),
                )]
                if len(closes) < 50 or _compute_ema is None:
                    continue
                ema50 = _last_non_none(_compute_ema(closes, 50))
                if ema50 is None:
                    continue
                total += 1
                if closes[-1] > ema50:
                    above += 1
            if total >= 10:
                univ_pct = round(above / total, 4)
        except Exception as exc:
            print(f"[regime] universe breadth failed: {exc}")

        if spy is None or soxx is None:
            return {**base, "spy": spy, "soxx": soxx, "qqq": qqq,
                    "universe_above_ema50_pct": univ_pct}

        if not spy["above"] or soxx["close"] < soxx["ema200"] * 0.97:
            regime = "bear"
            note = ("空頭環境：大盤或半導體基準跌破長期均線。動能訊號（OVERBOUGHT）"
                    "在此環境未經驗證，買進建議信心度應下調。")
        elif spy["above"] and soxx["above"] and univ_pct is not None and univ_pct > 0.5:
            regime = "bull"
            note = ("多頭環境：基準指數站上 EMA200 且過半個股站上 EMA50。"
                    "既有樣本外驗證主要來自此環境。")
        else:
            regime = "neutral"
            note = ("中性/轉折環境：基準與個股廣度訊號不一致。"
                    "建議降低倉位並提高對訊號的懷疑度。")

        return {
            "as_of": as_of,
            "regime": regime,
            "spy": spy, "soxx": soxx, "qqq": qqq,
            "universe_above_ema50_pct": univ_pct,
            "note": note,
        }

    except Exception as exc:
        print(f"[regime_payload] error: {exc}")
        return base


# ---------------------------------------------------------------------------
# R3-1: Hit-rate API helpers
# ---------------------------------------------------------------------------

# Lock to prevent concurrent hitrate reconstruction
_hitrate_lock = threading.Lock()


def _compute_live_hitrate(con) -> dict:
    """
    Compute hit rates from live signal_history rows (accumulated since 2026-07-04).

    A row is "matured" when a price 30 calendar days after its date is available.
    hit definition:
      BUY_TRIGGER / BUY_WATCH: hit = fwd_return > universe median on that date
      EXIT_ALERT:              hit = fwd_return < universe median on that date
      HOLD / NEUTRAL / OVERBOUGHT: excluded from hit counting (shown in summary only)

    Returns the "live" section of the hitrate response.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")

    try:
        rows = con.execute("""
            select symbol, date, signal, close as entry_close
            from signal_history
            where symbol not in ('SPY', 'SOXX', 'QQQ')
              and close is not null and close > 0
            order by date
        """).fetchall()
    except Exception as exc:
        print(f"[hitrate live] query failed: {exc}")
        return {"label": "live (signal_history)", "rows": [], "n_total": 0, "as_of": as_of}

    if not rows:
        return {"label": "live (signal_history)", "rows": [], "n_total": 0, "as_of": as_of}

    # Build per-date universe medians (lazy computed)
    date_univ_med: dict = {}

    def _get_univ_median(date_str: str):
        if date_str in date_univ_med:
            return date_univ_med[date_str]
        try:
            # For each signal_history row on this date, compute 30d forward return
            sh_rows = con.execute("""
                select symbol, close
                from signal_history
                where date=? and close is not null and close > 0
                  and symbol not in ('SPY', 'SOXX', 'QQQ')
            """, (date_str,)).fetchall()
            rets = []
            for sr in sh_rows:
                sym, entry = sr[0], sr[1]
                exit_row = con.execute("""
                    select close from prices
                    where symbol=? and date > ? and date <= date(?, '+30 days')
                    order by date desc limit 1
                """, (sym, date_str, date_str)).fetchone()
                if exit_row and exit_row[0] and entry > 0:
                    rets.append(exit_row[0] / entry - 1.0)
            med = None
            if rets:
                srt = sorted(rets)
                mid = len(srt) // 2
                med = srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2
            date_univ_med[date_str] = med
        except Exception:
            date_univ_med[date_str] = None
        return date_univ_med[date_str]

    # Per-signal buckets + per-call records (for the recent_calls contract field)
    _EXCLUDED_SIGNALS = {"HOLD", "NEUTRAL", "OVERBOUGHT"}
    from collections import defaultdict
    buckets: dict = defaultdict(lambda: {"n": 0, "matured": 0, "hits": 0, "excess": [], "fwds": []})
    calls: list = []

    for row in rows:
        sym, date_str, sig, entry_close = row[0], row[1], row[2], row[3]

        if sig in _EXCLUDED_SIGNALS:
            continue

        buckets[sig]["n"] += 1

        # Find exit close
        try:
            exit_row = con.execute("""
                select close from prices
                where symbol=? and date > ? and date <= date(?, '+30 days')
                order by date desc limit 1
            """, (sym, date_str, date_str)).fetchone()
        except Exception:
            exit_row = None

        exit_close = exit_row[0] if exit_row else None
        univ_med = _get_univ_median(date_str) if exit_close is not None else None

        fwd_return = None
        hit = None
        if exit_close is not None and univ_med is not None:
            fwd_return = exit_close / entry_close - 1.0
            buckets[sig]["matured"] += 1
            buckets[sig]["excess"].append(fwd_return - univ_med)
            buckets[sig]["fwds"].append(fwd_return)
            if sig in ("BUY_TRIGGER", "BUY_WATCH"):
                hit = fwd_return > univ_med
            elif sig == "EXIT_ALERT":
                hit = fwd_return < univ_med
            if hit:
                buckets[sig]["hits"] += 1

        calls.append({
            "symbol": sym,
            "date": date_str,
            "signal": sig,
            "close_then": entry_close,
            "close_now": exit_close,
            "fwd_return": round(fwd_return, 4) if fwd_return is not None else None,
            "universe_return": round(univ_med, 4) if univ_med is not None else None,
            "hit": hit,
            "source": "live",
        })

    def _median(vals):
        if not vals:
            return None
        s = sorted(vals)
        m = len(s) // 2
        return s[m] if len(s) % 2 == 1 else (s[m - 1] + s[m]) / 2

    summary_rows = []
    for sig, b in buckets.items():
        n_matured = b["matured"]
        hits = b["hits"]
        insufficient = n_matured < 10
        win_rate = None if (insufficient or n_matured == 0) else round(hits / n_matured, 4)
        med_exc = _median(b["excess"])
        med_fwd = _median(b["fwds"])
        summary_rows.append({
            "signal":      sig,
            "n":           b["n"],
            "n_matured":   n_matured,
            "hits":        hits if not insufficient else None,
            "win_rate":    win_rate,
            "median_fwd_return_30d": round(med_fwd, 4) if (med_fwd is not None and not insufficient) else None,
            "vs_universe": round(med_exc, 4) if (med_exc is not None and not insufficient) else None,
            "insufficient": insufficient,
        })

    # Sort by signal name for deterministic output
    summary_rows.sort(key=lambda x: x["signal"])
    calls.sort(key=lambda c: c["date"], reverse=True)

    return {
        "label":   "live (signal_history, 開始日期 2026-07-04)",
        "rows":    summary_rows,
        "calls":   calls,
        "n_total": len(rows),
        "as_of":   as_of,
    }


def _compute_reconstructed_hitrate(con, db_path: Path) -> dict:
    """
    Reconstruct hit rates using the multiwindow point-in-time machinery.

    Reuses backtest_multiwindow.py's evaluate_symbol_at_cutoff discipline:
    - Bars truncated to <= cutoff before indicator computation (zero look-ahead)
    - score_symbol called with now=cutoff
    - Exit = last close within (cutoff, cutoff + 30 days]

    Results are cached in hitrate_cache; invalidated when max price date advances.

    hit definition (per spec):
      BUY_TRIGGER / BUY_WATCH: fwd_return > universe median same window
      EXIT_ALERT:              fwd_return < universe median same window
      HOLD / NEUTRAL / OVERBOUGHT: excluded from hit counting
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    _EXCLUDED = {"HOLD", "NEUTRAL", "OVERBOUGHT"}
    empty = {
        "label": "reconstructed (point-in-time multiwindow)",
        "cutoff_count": 0,
        "total_obs": 0,
        "rows": [],
        "as_of": as_of,
    }

    # Current max price date (non-benchmark)
    try:
        row = con.execute(
            "select max(date) from prices where symbol not in ('SPY','SOXX','QQQ')"
        ).fetchone()
        max_price_date = row[0] if row and row[0] else None
    except Exception:
        max_price_date = None

    if not max_price_date:
        return {**empty, "note": "No price data available for reconstruction."}

    # Check cache (v2: rows carry vs_universe median excess)
    try:
        cached = con.execute(
            "select cache_json, max_price_date from hitrate_cache where cache_key='reconstructed_v2'"
        ).fetchone()
        if cached and cached[1] == max_price_date:
            try:
                return json.loads(cached[0])
            except Exception:
                pass
    except Exception:
        pass

    # Load multiwindow module (lazy, so server startup stays fast)
    try:
        import importlib.util as _mw_ilu
        _mw_spec = _mw_ilu.spec_from_file_location(
            "backtest_multiwindow",
            Path(__file__).resolve().parent / "backtest_multiwindow.py",
        )
        _mw_mod = _mw_ilu.module_from_spec(_mw_spec)
        _mw_spec.loader.exec_module(_mw_mod)
    except Exception as exc:
        print(f"[hitrate recon] Failed to load backtest_multiwindow: {exc}")
        return {**empty, "note": f"Reconstruction unavailable: {exc}"}

    try:
        import sqlite3 as _sq3
        _recon_con = _sq3.connect(str(db_path))
        _recon_con.row_factory = _sq3.Row

        # Load all prices, excluding benchmarks
        all_rows = _recon_con.execute(
            "SELECT symbol, date, open, high, low, close, volume "
            "FROM prices "
            "WHERE close IS NOT NULL AND close > 0 "
            "  AND symbol NOT IN ('SPY', 'SOXX', 'QQQ') "
            "ORDER BY symbol, date"
        ).fetchall()
        _recon_con.close()
    except Exception as exc:
        print(f"[hitrate recon] Failed to load prices: {exc}")
        return {**empty, "note": f"Price load failed: {exc}"}

    # Build all_prices dict
    all_prices: dict = {}
    for r in all_rows:
        sym = r[0]
        if sym not in all_prices:
            all_prices[sym] = []
        all_prices[sym].append({
            "symbol": sym, "date": r[1],
            "open": r[2], "high": r[3], "low": r[4],
            "close": r[5], "volume": r[6],
        })

    if not all_prices:
        return {**empty, "note": "No price data for reconstruction."}

    # Enumerate cutoffs (every 7 days, matching spec)
    try:
        cutoffs = _mw_mod._enumerate_cutoffs(
            all_prices, max_price_date,
            step_days=7, min_symbols=10, min_bars=60, horizon_days=30,
        )
    except Exception as exc:
        print(f"[hitrate recon] cutoff enumeration failed: {exc}")
        cutoffs = []

    if not cutoffs:
        result = {**empty, "note": "Insufficient history to enumerate cutoffs (need ≥10 symbols with ≥60 bars)."}
        return result

    print(f"[hitrate recon] Running {len(cutoffs)} cutoffs (step=7d, horizon=30d)…")

    # Per-signal accumulators
    from collections import defaultdict
    buckets: dict = defaultdict(lambda: {"n_total": 0, "returns": [], "excess": [], "hits": 0})
    total_obs = 0

    for cutoff_str in cutoffs:
        try:
            win = _mw_mod.run_window_fixed_horizon(
                db_path, all_prices, cutoff_str, horizon_days=30, min_bars=60
            )
        except Exception as exc:
            print(f"[hitrate recon] window {cutoff_str} failed: {exc}")
            continue

        records = win.get("records", [])
        # Universe returns for this window
        win_rets = [r["holdout_return"] for r in records if r.get("holdout_return") is not None]
        if not win_rets:
            continue
        srt = sorted(win_rets)
        mid = len(srt) // 2
        univ_med = srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2

        for rec in records:
            sig = rec.get("signal")
            hret = rec.get("holdout_return")
            if sig is None:
                continue
            if sig in _EXCLUDED:
                continue
            total_obs += 1
            buckets[sig]["n_total"] += 1
            if hret is not None:
                buckets[sig]["returns"].append(hret)
                buckets[sig]["excess"].append(hret - univ_med)
                if sig in ("BUY_TRIGGER", "BUY_WATCH") and hret > univ_med:
                    buckets[sig]["hits"] += 1
                elif sig == "EXIT_ALERT" and hret < univ_med:
                    buckets[sig]["hits"] += 1

    def _median(vals):
        if not vals:
            return None
        s = sorted(vals)
        m = len(s) // 2
        return s[m] if len(s) % 2 == 1 else (s[m - 1] + s[m]) / 2

    summary_rows = []
    for sig, b in buckets.items():
        n = len(b["returns"])
        hits = b["hits"]
        insufficient = n < 10
        win_rate = None if (insufficient or n == 0) else round(hits / n, 4)
        med_exc = _median(b["excess"])
        med_fwd = _median(b["returns"])
        summary_rows.append({
            "signal":      sig,
            "n":           b["n_total"],
            "n_with_exit": n,
            "hits":        hits if not insufficient else None,
            "win_rate":    win_rate,
            "median_fwd_return_30d": round(med_fwd, 4) if (med_fwd is not None and not insufficient) else None,
            "vs_universe": round(med_exc, 4) if (med_exc is not None and not insufficient) else None,
            "insufficient": insufficient,
        })

    summary_rows.sort(key=lambda x: x["signal"])

    result = {
        "label":        "reconstructed (point-in-time multiwindow)",
        "cutoff_count": len(cutoffs),
        "total_obs":    total_obs,
        "rows":         summary_rows,
        "as_of":        as_of,
    }

    # Cache result
    try:
        con.execute(
            """insert into hitrate_cache(cache_key, max_price_date, cache_json, computed_at)
               values ('reconstructed_v2', ?, ?, ?)
               on conflict(cache_key) do update set
                   max_price_date=excluded.max_price_date,
                   cache_json=excluded.cache_json,
                   computed_at=excluded.computed_at""",
            (max_price_date, json.dumps(result, ensure_ascii=False), datetime.now().isoformat()),
        )
        con.commit()
        print(f"[hitrate recon] Cached reconstruction result ({len(cutoffs)} cutoffs, {total_obs} obs)")
    except Exception as exc:
        print(f"[hitrate recon] Cache write failed: {exc}")

    return result


def hitrate_payload(con) -> dict:
    """
    GET /api/hitrate

    Returns hit-rate analysis from two honestly labeled sources:
      "live":         signal_history rows (started 2026-07-04) with 30d forward
      "reconstructed": point-in-time multiwindow reconstruction (cached)

    Never 500s — degrades gracefully to empty rows.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    try:
        live = _compute_live_hitrate(con)
    except Exception as exc:
        print(f"[hitrate_payload] live source failed: {exc}")
        live = {"label": "live (signal_history)", "rows": [], "n_total": 0, "as_of": as_of, "error": str(exc)}

    with _hitrate_lock:
        try:
            recon = _compute_reconstructed_hitrate(con, DB_PATH)
        except Exception as exc:
            print(f"[hitrate_payload] reconstructed source failed: {exc}")
            recon = {
                "label": "reconstructed (point-in-time multiwindow)",
                "cutoff_count": 0,
                "total_obs": 0,
                "rows": [],
                "as_of": as_of,
                "error": str(exc),
            }

    # Assemble the spec contract (REQUIREMENTS_V3.md R3-1):
    # {"as_of","live_since","summary":[...rows with source...],"recent_calls":[...]}
    live_since = None
    try:
        row = con.execute("select min(date) from signal_history").fetchone()
        live_since = row[0] if row else None
    except Exception:
        pass

    summary = []
    for r in live.get("rows", []):
        summary.append({
            "signal": r.get("signal"),
            "n": r.get("n_matured", r.get("n")),
            "n_pending": (r.get("n") or 0) - (r.get("n_matured") or 0),
            "median_fwd_return_30d": r.get("median_fwd_return_30d"),
            "win_rate": r.get("win_rate"),
            "vs_universe": r.get("vs_universe"),
            "insufficient": r.get("insufficient", False),
            "source": "live",
        })
    for r in recon.get("rows", []):
        summary.append({
            "signal": r.get("signal"),
            "n": r.get("n_with_exit", r.get("n")),
            "n_pending": 0,
            "median_fwd_return_30d": r.get("median_fwd_return_30d"),
            "win_rate": r.get("win_rate"),
            "vs_universe": r.get("vs_universe"),
            "insufficient": r.get("insufficient", False),
            "source": "reconstructed",
        })

    return {
        "as_of": as_of,
        "live_since": live_since,
        "summary": summary,
        "recent_calls": live.get("calls", [])[:50],
        # Kept for transparency/debugging: full per-source detail
        "sources": {
            "live":          {k: v for k, v in live.items() if k != "calls"},
            "reconstructed": recon,
        },
    }


# ---------------------------------------------------------------------------
# R3-3: Analyst estimates helpers
# ---------------------------------------------------------------------------

def estimates_payload(con, symbol: str) -> dict:
    """
    GET /api/estimates/<SYM>

    Returns analyst estimates from the analyst_estimates table.
    Derived field "revision_direction":
      "up"     — 30d up-revisions > down-revisions
      "down"   — down-revisions > up-revisions
      "neutral"— equal (both non-zero or both zero)
      null     — when up_revisions_30d or down_revisions_30d is NULL

    Also returns "target_vs_price": (target_mean / latest_close - 1) when available.
    Returns sane empty structure (all nulls) when table absent or symbol not found.
    """
    base: dict = {
        "symbol":                  symbol,
        "target_mean":             None,
        "target_median":           None,
        "target_high":             None,
        "target_low":              None,
        "n_analysts":              None,
        "recommendation_key":      None,
        "recommendation_mean":     None,
        "eps_estimate_current_q":  None,
        "eps_estimate_next_q":     None,
        "eps_estimate_current_y":  None,
        "up_revisions_30d":        None,
        "down_revisions_30d":      None,
        "revision_direction":      None,
        "target_vs_price":         None,
        "updated_at":              None,
    }

    if not _table_exists(con, "analyst_estimates"):
        return base

    try:
        row = con.execute(
            """select symbol, target_mean, target_median, target_high, target_low,
                      n_analysts, recommendation_key, recommendation_mean,
                      eps_estimate_current_q, eps_estimate_next_q, eps_estimate_current_y,
                      up_revisions_30d, down_revisions_30d, updated_at
               from analyst_estimates where symbol=?""",
            (symbol,),
        ).fetchone()

        if not row:
            return base

        result = dict(row)

        # Derive revision_direction
        up = result.get("up_revisions_30d")
        down = result.get("down_revisions_30d")
        if up is not None and down is not None:
            if up > down:
                result["revision_direction"] = "up"
            elif down > up:
                result["revision_direction"] = "down"
            else:
                result["revision_direction"] = "neutral"
        else:
            result["revision_direction"] = None

        # Derive target_vs_price
        target_mean = result.get("target_mean")
        if target_mean is not None:
            try:
                price_row = con.execute(
                    "select close from prices where symbol=? order by date desc limit 1", (symbol,)
                ).fetchone()
                if price_row and price_row[0] and price_row[0] > 0:
                    result["target_vs_price"] = round(target_mean / price_row[0] - 1.0, 4)
            except Exception:
                pass

        return result

    except Exception as exc:
        print(f"[estimates_payload] {symbol}: {exc}")
        return base


# ---------------------------------------------------------------------------
# R5-3: Expert views API helpers
# ---------------------------------------------------------------------------

def _expert_views_row_to_item(r) -> dict:
    return {
        "source":       r["source"],
        "author":       r["author"],
        "title":        r["title"],
        "text":         r["text"],
        "url":          r["url"],
        "published_at": r["published_at"],
        "credibility":  r["credibility"],
    }


def expert_views_payload(con, symbol: str) -> dict:
    """
    GET /api/expert-views/<SYM>
    Returns up to 10 items mentioning this symbol, newest first.
    Returns empty structure when table absent (graceful degradation).
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    empty = {"symbol": symbol, "items": [], "as_of": as_of}

    if not _table_exists(con, "expert_views"):
        return empty

    try:
        sym_json = symbol  # we search inside the JSON array string
        rows = con.execute(
            """select source, author, title, text, url, published_at, credibility
               from expert_views
               where symbols like ?
                  or symbols like ?
                  or symbols like ?
                  or symbols like ?
               order by published_at desc
               limit 10""",
            (
                f'["{sym_json}"]',
                f'["{sym_json}",%',
                f'%,"{sym_json}",%',
                f'%,"{sym_json}"]',
            ),
        ).fetchall()
        items = [_expert_views_row_to_item(r) for r in rows]
        return {"symbol": symbol, "items": items, "as_of": as_of}
    except Exception as exc:
        print(f"[expert_views_payload] {symbol}: {exc}")
        return empty


def expert_views_all_payload(con) -> dict:
    """
    GET /api/expert-views
    Returns latest 20 items across all symbols, newest first.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    empty = {"items": [], "as_of": as_of}

    if not _table_exists(con, "expert_views"):
        return empty

    try:
        rows = con.execute(
            """select source, author, title, text, url, published_at, credibility
               from expert_views
               order by published_at desc
               limit 20"""
        ).fetchall()
        items = [_expert_views_row_to_item(r) for r in rows]
        return {"items": items, "as_of": as_of}
    except Exception as exc:
        print(f"[expert_views_all_payload]: {exc}")
        return empty


# ---------------------------------------------------------------------------
# R3-5: Signal changes helpers
# ---------------------------------------------------------------------------

def changes_payload(con, days: int = 7) -> dict:
    """
    GET /api/changes?days=N

    Returns signal transitions from signal_changes table, newest first.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    empty = {"days": days, "items": [], "as_of": as_of}

    try:
        if not _table_exists(con, "signal_changes"):
            return empty

        rows = con.execute(
            """select symbol, date, prev_signal, new_signal
               from signal_changes
               where date >= date('now', ? || ' days')
               order by date desc, symbol""",
            (f"-{days}",),
        ).fetchall()

        return {
            "days":  days,
            "items": [dict(r) for r in rows],
            "as_of": as_of,
        }

    except Exception as exc:
        print(f"[changes_payload] error: {exc}")
        return {**empty, "error": str(exc)}


_RELIABILITY_NOTE = (
    "Multi-window out-of-sample validation (21 cutoffs, fixed 30-day horizon, "
    "1541 observations) found: BUY_WATCH UNDERPERFORMS the universe (-4.8pp, "
    "n=35); chasing extended high-heat stocks is a confirmed drag (-5.2pp, "
    "n=30); EXIT_ALERT shows NO edge at scale (-0.9pp, n=488 — the earlier "
    "+13pp result was period noise); a pullback-entry variant looks promising "
    "(+71% win rate) but n=7 is insufficient to conclude. Observations overlap "
    "across windows, so effective sample sizes are smaller than stated. See "
    "docs/VALIDATION.md; reproduce with scripts/backtest_multiwindow.py. This "
    "is not investment advice."
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

    # --- 9b. Fundamentals section (R2-4) ---
    fundamentals_section = None
    try:
        if _table_exists(con, "fundamentals"):
            frow = con.execute(
                """select pe, forward_pe, eps_ttm, revenue_growth_yoy,
                          gross_margin, market_cap, next_earnings_date, updated_at
                   from fundamentals where symbol=?""",
                (symbol,),
            ).fetchone()
            if frow:
                fundamentals_section = dict(frow)
    except Exception as exc:
        print(f"[Dossier] fundamentals fetch failed for {symbol}: {exc}")

    # --- 9c. News section (R2-4) ---
    news_section = {"items": [], "macro": []}
    try:
        if _table_exists(con, "news"):
            sym_rows = con.execute(
                """select title, source, url, published_at, summary
                   from news
                   where scope='symbol'
                     and (symbols like ? or symbols like ? or symbols like ? or symbols like ?)
                     and published_at >= datetime('now', '-7 days')
                   order by published_at desc
                   limit 10""",
                (
                    f'["{symbol}"]',
                    f'"{symbol}",%',
                    f'%,"{symbol}",%',
                    f'%,"{symbol}"]',
                ),
            ).fetchall()
            news_section["items"] = [dict(r) for r in sym_rows]

            macro_rows = con.execute(
                """select title, source, url, published_at, summary
                   from news
                   where scope='macro'
                     and published_at >= datetime('now', '-3 days')
                   order by published_at desc
                   limit 5""",
            ).fetchall()
            news_section["macro"] = [dict(r) for r in macro_rows]
    except Exception as exc:
        print(f"[Dossier] news fetch failed for {symbol}: {exc}")

    # --- 9d. Regime section (R3-2) ---
    regime_section = None
    try:
        regime_section = regime_payload(con)
    except Exception as exc:
        print(f"[Dossier] regime fetch failed: {exc}")

    # --- 9e. Analyst estimates section (R3-3) ---
    estimates_section = None
    try:
        estimates_section = estimates_payload(con, symbol)
        # Only include if at least one non-null field exists (besides symbol/updated_at)
        _est_vals = [
            estimates_section.get(k) for k in (
                "target_mean", "n_analysts", "recommendation_key",
                "eps_estimate_current_q", "revision_direction"
            )
        ]
        if not any(v is not None for v in _est_vals):
            estimates_section = None
    except Exception as exc:
        print(f"[Dossier] estimates fetch failed for {symbol}: {exc}")

    # --- 10. Build Gemini prompt and call for manager_view ---
    manager_view = None
    if _key_manager.has_any_key():
        try:
            current_regime = (regime_section or {}).get("regime", "unknown")
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
                "fundamentals": fundamentals_section,
                "recent_symbol_news_titles": [
                    n["title"] for n in news_section["items"]
                ],
                "recent_macro_news_titles": [
                    n["title"] for n in news_section["macro"]
                ],
                # R3-2: regime context
                "market_regime": current_regime,
                "regime_data": {
                    "spy": (regime_section or {}).get("spy"),
                    "soxx": (regime_section or {}).get("soxx"),
                    "universe_above_ema50_pct": (regime_section or {}).get("universe_above_ema50_pct"),
                    "note": (regime_section or {}).get("note"),
                } if regime_section else None,
                # R3-3: analyst estimates
                "analyst_estimates": {
                    k: estimates_section.get(k)
                    for k in (
                        "target_mean", "target_median", "n_analysts",
                        "recommendation_key", "recommendation_mean",
                        "eps_estimate_current_q", "eps_estimate_next_y",
                        "up_revisions_30d", "down_revisions_30d",
                        "revision_direction", "target_vs_price",
                    )
                } if estimates_section else None,
            }

            # Bear-regime prompt instruction (zh-TW, injected into system prompt)
            _bear_warning = ""
            if current_regime == "bear":
                _bear_warning = (
                    "\n\n【熊市警告 — 必須執行】目前 SPY 市場環境判斷為「熊市（bear）」："
                    "SPY 收盤價與 EMA50 均低於 EMA200。在此環境下，你必須："
                    "（1）將 conviction 調降至 LOW 或最多 MEDIUM（除非有壓倒性多頭證據）；"
                    "（2）在 bear_case 與 position_guidance 中明確加入動能下行風險警語（以繁體中文撰寫）；"
                    "（3）recommendation 不得輸出 ACCUMULATE，優先考慮 WATCH 或 AVOID。"
                )

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
                + _bear_warning
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
                task_class="interactive",
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
        "fundamentals": fundamentals_section,
        "news": news_section,
        "regime": regime_section,
        "estimates": estimates_section,
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
    R-5 / R3-5: Upsert today's signal row for every symbol that has price data.

    Idempotent — running twice on the same day overwrites the row with
    identical data, leaving the table consistent.  Reuses signal_payload
    so all computation is DRY.

    Also writes to signal_changes when the signal transitions from the most
    recent prior snapshot (R3-5).

    Benchmark symbols (SPY/SOXX/QQQ) are excluded from universe snapshots.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    con = db()
    try:
        symbols = [
            r[0] for r in con.execute(
                "select distinct symbol from prices"
            ).fetchall()
            if r[0] not in BENCHMARK_SYMBOLS
        ]
        inserted = 0
        changes_written = 0
        for sym in symbols:
            try:
                sp = signal_payload(con, sym)
                # D-2: read the structured rsi field directly (primary path).
                # Fall back to condition-text parsing only when the field is
                # absent (defensive: supports payloads from older code paths).
                rsi_val = sp.get("rsi")
                if rsi_val is None and sp.get("conditions"):
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

                new_signal = sp.get("signal")

                # R3-5: detect signal change vs most recent prior snapshot
                try:
                    prior_row = con.execute(
                        "select signal from signal_history where symbol=? and date<? order by date desc limit 1",
                        (sym, today),
                    ).fetchone()
                    if prior_row is not None:
                        prev_signal = prior_row[0]
                        if prev_signal != new_signal:
                            con.execute(
                                """insert into signal_changes(symbol, date, prev_signal, new_signal)
                                   values (?, ?, ?, ?)
                                   on conflict(symbol, date) do update set
                                       prev_signal=excluded.prev_signal,
                                       new_signal=excluded.new_signal""",
                                (sym, today, prev_signal, new_signal),
                            )
                            changes_written += 1
                except Exception as chg_exc:
                    print(f"[Snapshot] signal_changes write failed for {sym}: {chg_exc}")

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
                        new_signal,
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
        print(f"[Snapshot] signal_history upserted {inserted} rows for {today}; "
              f"{changes_written} signal changes recorded.")
        return inserted
    finally:
        con.close()


# ---------------------------------------------------------------------------
# V6 Arena API payloads (accept external con for testability)
# ---------------------------------------------------------------------------

def arena_leaderboard_payload(con, month: str) -> dict:
    """
    GET /api/arena/leaderboard?month=YYYY-MM
    Live leaderboard computed directly from agent_nav_daily (return vs. the 3000
    seed) plus filled-trade counts — so it populates every trading day without
    waiting for month-end settlement (agent_monthly). Values are traceable to real
    NAV rows; agents with no NAV row for the month are omitted (insufficient data).
    """
    SEED = 3000.0
    try:
        agent_rows = con.execute(
            "select id as agent_id, domain from agents order by id"
        ).fetchall()
    except Exception:
        agent_rows = []

    computed = []
    for a in agent_rows:
        agent_id = a["agent_id"]
        navs = con.execute(
            "select date, nav from agent_nav_daily "
            "where agent_id=? and date like ? order by date",
            (agent_id, month + "%")
        ).fetchall()
        if not navs:
            continue

        latest_nav = navs[-1]["nav"]
        ret_pct = (latest_nav / SEED - 1.0) * 100.0

        # Max drawdown over the month's NAV path (<= 0)
        peak = None
        mdd = 0.0
        for r in navs:
            v = r["nav"]
            if peak is None or v > peak:
                peak = v
            if peak:
                dd = (v / peak - 1.0) * 100.0
                if dd < mdd:
                    mdd = dd

        # Count filled trades by decision month so this number matches the
        # trades-log panel (arena_trades_payload also filters on decided_date).
        n_trades = con.execute(
            "select count(*) from agent_trades "
            "where agent_id=? and decided_date like ? and status='filled'",
            (agent_id, month + "%")
        ).fetchone()[0]

        computed.append({
            "agent_id":   agent_id,
            "domain":     a["domain"],
            "ret_pct":    ret_pct,
            "mdd_pct":    mdd,
            "n_trades":   n_trades,
            "latest_nav": latest_nav,
        })

    # Overall rank by return descending
    computed.sort(key=lambda x: x["ret_pct"], reverse=True)
    for i, c in enumerate(computed, 1):
        c["rank_overall"] = i

    # Domain rank (list already ordered by return desc → per-domain order is correct)
    dom_seen = {}
    for c in computed:
        dom_seen[c["domain"]] = dom_seen.get(c["domain"], 0) + 1
        c["rank_domain"] = dom_seen[c["domain"]]

    return {"month": month, "rows": computed}


def arena_nav_payload(con, month: str) -> dict:
    """
    GET /api/arena/nav?month=YYYY-MM
    Returns NAV time series for all agents in the month, plus SPY benchmark.
    """
    series = {}
    try:
        # Drive off the agents table (same source as the leaderboard) so both
        # views agree on the agent roster; agents with no NAV row are skipped below.
        agents = con.execute("select id as agent_id from agents order by id").fetchall()
        for ag in agents:
            agent_id = ag["agent_id"]
            rows = con.execute(
                "select date, nav from agent_nav_daily "
                "where agent_id=? and date like ? order by date",
                (agent_id, month + "%")
            ).fetchall()
            if rows:
                series[agent_id] = [{"date": r["date"], "nav": r["nav"]} for r in rows]
    except Exception:
        pass

    # SPY benchmark — normalized to 3000 starting point for comparability
    benchmark = {}
    try:
        spy_rows = con.execute(
            "select date, close from prices where symbol='SPY' and date like ? order by date",
            (month + "%",)
        ).fetchall()
        if spy_rows:
            base_close = spy_rows[0]["close"]
            spy_series = []
            for r in spy_rows:
                normalized = (r["close"] / base_close) * 3000.0 if base_close else r["close"]
                spy_series.append({"date": r["date"], "nav": normalized})
            benchmark["SPY"] = spy_series
    except Exception:
        pass

    return {"series": series, "benchmark": benchmark}


def arena_trades_payload(con, agent_id: str, month: str) -> dict:
    """
    GET /api/arena/trades?agent=<id>&month=YYYY-MM
    Returns all trades for an agent in the given month.
    """
    try:
        rows = con.execute(
            "select decided_date, exec_date, symbol, side, qty, price, usd, "
            "       reason, status, rejected_reason "
            "from agent_trades "
            "where agent_id=? and decided_date like ? "
            "order by id",
            (agent_id, month + "%")
        ).fetchall()
    except Exception:
        rows = []

    trades = []
    for r in rows:
        trades.append({
            "decided_date":   r["decided_date"],
            "exec_date":      r["exec_date"],
            "symbol":         r["symbol"],
            "side":           r["side"],
            "qty":            r["qty"],
            "price":          r["price"],
            "usd":            r["usd"],
            "reason":         r["reason"],
            "status":         r["status"],
            "rejected_reason": r["rejected_reason"],
        })
    return {"trades": trades}


def arena_reflections_payload(con, month: str) -> dict:
    """
    GET /api/arena/reflections?month=YYYY-MM
    Returns reflection data for all agents in the given month.
    """
    try:
        rows = con.execute(
            "select agent_id, public_letter, reflection_md, strategy_after "
            "from agent_monthly where month=? order by agent_id",
            (month,)
        ).fetchall()
    except Exception:
        rows = []

    result_rows = []
    for r in rows:
        result_rows.append({
            "agent_id":      r["agent_id"],
            "public_letter": r["public_letter"],
            "reflection_md": r["reflection_md"],
            "strategy_after": r["strategy_after"],
        })
    return {"rows": result_rows}


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
