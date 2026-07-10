"""
serenity/api/handler.py
Handler 類、send_json、route_api、route_post_api
（原 server.py 469-844 行）
"""
import json
import os
from datetime import datetime
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from ..config import DB_PATH, ROOT, STATIC_DIR
from ..db import db
from ..keypool import _key_manager
from ..services.market import (
    changes_payload, estimates_payload, fundamentals_payload,
    news_payload, summary, symbol_payload,
)
from ..services.signal import signal_payload
from ..services.regime import regime_payload
from ..services.hitrate import hitrate_payload
from ..services.experts import expert_views_all_payload, expert_views_payload
from ..services.dossier import dossier_payload
from ..services.arena_views import (
    arena_leaderboard_payload, arena_nav_payload,
    arena_trades_payload, arena_reflections_payload,
)
from ..services.chat import handle_chat_api
from ..services.translate import handle_translate_api
from ..services.scorecard import generate_scorecard
from ..services.settings import (
    build_settings_response,
    handle_post_settings,
    handle_test_key,
)
from ..services.bootstrap import get_status as bootstrap_get_status, handle_post_bootstrap


class _BadRequest(Exception):
    """路由回傳 400 Bad Request 時拋出。"""


class _HTTPResponse(Exception):
    """路由需要自訂 HTTP status code 時拋出（payload, status）。"""
    def __init__(self, payload: dict, status: int):
        self.payload = payload
        self.status = status


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
            except _HTTPResponse as exc:
                self.send_json(exc.payload, status=exc.status)
            except _BadRequest as exc:
                self.send_json({"error": str(exc)}, status=400)
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
        # 階段三：Bootstrap API
        if path == "/api/admin/bootstrap/status":
            return bootstrap_get_status()
        # 階段二：設定 API
        if path == "/api/settings":
            return build_settings_response()
        if path == "/api/config":
            from ..config import get_setting
            return {
                "has_key": bool(get_setting("gemini_api_key")),
                "default_model": get_setting("gemini_model"),
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
        # 階段三：Bootstrap API
        if path == "/api/admin/bootstrap":
            resp, status = handle_post_bootstrap(payload)
            raise _HTTPResponse(resp, status)
        # 階段二：設定 API
        if path == "/api/settings":
            try:
                return handle_post_settings(payload)
            except ValueError as e:
                raise _BadRequest(str(e))
        if path == "/api/settings/test":
            return handle_test_key(payload)
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
            return generate_scorecard(symbol)
        return {"error": "unknown api"}
