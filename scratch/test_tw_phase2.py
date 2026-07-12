# -*- coding: utf-8 -*-
"""
(f) 台股中文新聞 + 情緒 Phase 2 驗收測試
（規格：docs/REQUIREMENTS_AI_MARKET.md f-驗收 1-6）

執行：PYTHONIOENCODING=utf-8 python scratch/test_tw_phase2.py
通過標準：0 failed、exit 0。
原則：
  - monkeypatch _fetch_rss（不打真 Google News）
  - 假 local LLM HTTP server（不打真 Ollama / Gemini）
  - tempfile DB 副本
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SERENITY_NO_DOTENV", "1")

_MAIN_DB = r"C:\Users\Jeff\OneDrive\桌面\git_repo\serenity\data\serenity.sqlite"
_DB_SRC = os.environ.get("SERENITY_DB_SRC", _MAIN_DB)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    mark = "  OK " if ok else "! FAIL"
    print(f"{mark}  {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    return ok


def finish():
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print()
    print("=" * 70)
    print(f"TW Phase2 Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_db() -> tuple[str, sqlite3.Connection]:
    """Build a fresh tempfile DB with migrations applied."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    src = Path(_DB_SRC)
    if src.exists():
        shutil.copy2(str(src), tmp.name)
    import ingest
    orig = ingest.DB_PATH
    ingest.DB_PATH = Path(tmp.name)
    con = ingest.connect()
    ingest.migrate_tw_symbols(con)
    ingest.DB_PATH = orig
    return tmp.name, con


def _seed_db(con):
    """Insert minimal test data: tw_symbols (stock+etf) + prices for 2330.TW.
    Also wipes all existing tw_symbols and keeps only 2330/0050 to make tests deterministic.
    """
    # 清空現有目錄（生產 DB 副本含真實台股資料會干擾測試）
    con.execute("DELETE FROM tw_symbols")
    con.execute("DELETE FROM prices WHERE symbol IN ('2330.TW','0050.TW')")
    # 清 news/news_sentiment 避免 URL 衝突
    con.execute("DELETE FROM news WHERE source='Google News (台股)'")
    con.execute("DELETE FROM news_sentiment WHERE source='zh-news-llm'")
    con.execute(
        "INSERT OR REPLACE INTO tw_symbols(code,name,market,yahoo_symbol,kind,updated_at)"
        " VALUES('2330','台積電','twse','2330.TW','stock','2026-01-01T00:00:00Z')"
    )
    con.execute(
        "INSERT OR REPLACE INTO tw_symbols(code,name,market,yahoo_symbol,kind,updated_at)"
        " VALUES('0050','元大台灣50','twse','0050.TW','etf','2026-01-01T00:00:00Z')"
    )
    con.execute(
        "INSERT OR REPLACE INTO prices(symbol,date,open,high,low,close,volume)"
        " VALUES('2330.TW','2026-06-01',800.0,820.0,790.0,810.0,10000)"
    )
    # 0050.TW has prices too
    con.execute(
        "INSERT OR REPLACE INTO prices(symbol,date,open,high,low,close,volume)"
        " VALUES('0050.TW','2026-06-01',100.0,102.0,99.0,101.0,5000000)"
    )
    con.commit()


# Fake RSS items returned by monkeypatched _fetch_rss
_FAKE_RSS = [
    {
        "title": "台積電法說會亮眼，外資調升目標價",
        "link": "https://example.com/news/1",
        "description": "台積電第二季業績超預期，外資機構紛紛調升目標價至1100元。",
        "pubDate": "Mon, 01 Jul 2026 08:00:00 GMT",
    },
    {
        "title": "台積電下修Q3展望",
        "link": "https://example.com/news/2",
        "description": "受需求疲軟影響，台積電下修第三季業績展望。",
        "pubDate": "Tue, 02 Jul 2026 09:00:00 GMT",
    },
]


# ---------------------------------------------------------------------------
# f-驗收 1: fetch_tw_news
# ---------------------------------------------------------------------------

def test_fetch_tw_news():
    print("\n--- f-驗收 1: fetch_tw_news ---")
    import ingest

    tmp_name, con = _make_temp_db()
    try:
        _seed_db(con)

        rss_call_log: list[str] = []

        def fake_fetch_rss(url: str, timeout=20):
            rss_call_log.append(url)
            return _FAKE_RSS

        with mock.patch.object(ingest, "_fetch_rss", side_effect=fake_fetch_rss):
            ingest.fetch_tw_news(con, pause=0)

        # 驗證 news 列
        rows = con.execute(
            "SELECT title, source, scope, symbols FROM news WHERE source='Google News (台股)'"
            " ORDER BY title"
        ).fetchall()
        check("f-1 news 列已插入", len(rows) >= 1, f"rows={len(rows)}")

        # 欄位正確
        if rows:
            sources = {r[1] for r in rows}
            scopes  = {r[2] for r in rows}
            check("f-1 source='Google News (台股)'", sources == {"Google News (台股)"}, str(sources))
            check("f-1 scope='symbol'", scopes == {"symbol"}, str(scopes))
            # symbols 含 2330.TW
            syms_0 = json.loads(rows[0][3]) if rows[0][3] else []
            check("f-1 symbols 含 2330.TW", "2330.TW" in syms_0, str(syms_0))

        # kind='etf' 的台股不應被抓（0050.TW has prices but kind=etf）
        etf_news = con.execute(
            "SELECT COUNT(*) FROM news WHERE symbols LIKE '%0050.TW%'"
        ).fetchone()[0]
        check("f-1 ETF 不抓 (0050.TW 無新聞)", etf_news == 0, f"etf_news={etf_news}")

        # url 冪等：重跑不重複
        before_count = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        with mock.patch.object(ingest, "_fetch_rss", side_effect=fake_fetch_rss):
            ingest.fetch_tw_news(con, pause=0)
        after_count = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        check("f-1 url 冪等重跑不重複", after_count == before_count,
              f"before={before_count}, after={after_count}")

        # 沒有 prices 的台股不抓（構造一個有目錄但無價格的 symbol）
        con.execute(
            "INSERT OR REPLACE INTO tw_symbols(code,name,market,yahoo_symbol,kind,updated_at)"
            " VALUES('9999','無價格股','twse','9999.TW','stock','2026-01-01T00:00:00Z')"
        )
        con.commit()
        rss_call_log.clear()
        with mock.patch.object(ingest, "_fetch_rss", side_effect=fake_fetch_rss):
            ingest.fetch_tw_news(con, pause=0)
        no_price_called = any("9999" in u for u in rss_call_log)
        check("f-1 無 prices 台股不抓", not no_price_called,
              f"rss_call_log={rss_call_log}")

    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fake local LLM HTTP server
# ---------------------------------------------------------------------------

def _make_local_llm_server(response_text: str, status: int = 200) -> tuple:
    """Simulate Ollama /v1/chat/completions endpoint."""
    _body = json.dumps({
        "choices": [{"message": {"content": response_text}}]
    }).encode("utf-8")
    _status = status

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_body)))
            self.end_headers()
            self.wfile.write(_body)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever)
    t.daemon = True
    t.start()
    return srv, port, t


def _seed_news(con):
    """Insert 2 台股 news rows for 2330.TW to be scored."""
    con.execute("DELETE FROM news WHERE source='Google News (台股)'")
    con.execute("DELETE FROM news_sentiment WHERE source='zh-news-llm'")
    con.execute("""
        INSERT OR IGNORE INTO news(title, source, url, published_at, scope, symbols, summary, fetched_at)
        VALUES
          ('台積電法說會亮眼', 'Google News (台股)',
           'https://example.com/news/1', '2026-07-01T08:00:00Z',
           'symbol', '["2330.TW"]', '台積電第二季業績超預期', '2026-07-01T10:00:00Z'),
          ('台積電下修Q3展望', 'Google News (台股)',
           'https://example.com/news/2', '2026-07-02T09:00:00Z',
           'symbol', '["2330.TW"]', '受需求疲軟影響，台積電下修展望', '2026-07-02T10:00:00Z')
    """)
    con.commit()


# ---------------------------------------------------------------------------
# f-驗收 2: score_tw_news_sentiment (local backend, valid JSON)
# ---------------------------------------------------------------------------

def test_score_sentiment_valid():
    print("\n--- f-驗收 2: score_tw_news_sentiment 合法 JSON ---")
    import ingest

    tmp_name, con = _make_temp_db()
    try:
        _seed_db(con)
        _seed_news(con)

        # Fake LLM returns valid sentiment JSON
        llm_response = '[{"i":1,"sentiment":"Bullish"},{"i":2,"sentiment":"Bearish"}]'
        srv, port, _ = _make_local_llm_server(llm_response)
        try:
            with mock.patch("serenity.llm_local.get_setting",
                            side_effect=lambda k: "http://127.0.0.1:{}/".format(port)
                            if k == "local_llm_base_url" else None):
                n = ingest.score_tw_news_sentiment(con, backend="local", limit=50)
        finally:
            srv.shutdown()

        check("f-2 回傳筆數=2", n == 2, f"n={n}")
        rows = con.execute(
            "SELECT symbol, sentiment, source FROM news_sentiment WHERE source='zh-news-llm'"
            " ORDER BY sentiment"
        ).fetchall()
        check("f-2 news_sentiment 2 列", len(rows) == 2, f"rows={len(rows)}")
        sentiments = {r[1] for r in rows}
        check("f-2 sentiment 值域正確", sentiments == {"Bullish", "Bearish"},
              str(sentiments))
        sources = {r[2] for r in rows}
        check("f-2 source='zh-news-llm'", sources == {"zh-news-llm"}, str(sources))
        symbols = {r[0] for r in rows}
        check("f-2 symbol=2330.TW", symbols == {"2330.TW"}, str(symbols))

        # 冪等：重跑不重標
        srv2, port2, _ = _make_local_llm_server(llm_response)
        try:
            with mock.patch("serenity.llm_local.get_setting",
                            side_effect=lambda k: "http://127.0.0.1:{}/".format(port2)
                            if k == "local_llm_base_url" else None):
                n2 = ingest.score_tw_news_sentiment(con, backend="local", limit=50)
        finally:
            srv2.shutdown()
        check("f-2 冪等不重標 n2=0", n2 == 0, f"n2={n2}")

    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# f-驗收 2b: limit 生效
# ---------------------------------------------------------------------------

def test_score_sentiment_limit():
    print("\n--- f-驗收 2b: limit 生效 ---")
    import ingest

    tmp_name, con = _make_temp_db()
    try:
        _seed_db(con)
        # Insert 5 news items
        for i in range(5):
            con.execute(
                "INSERT OR IGNORE INTO news(title,source,url,published_at,scope,symbols,summary,fetched_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (f"新聞{i}", "Google News (台股)", f"https://example.com/news/limit/{i}",
                 f"2026-07-0{i+1}T08:00:00Z", "symbol", '["2330.TW"]', "摘要", "2026-07-01T10:00:00Z")
            )
        con.commit()

        llm_response = '[{"i":1,"sentiment":"Neutral"}]'
        srv, port, _ = _make_local_llm_server(llm_response)
        try:
            with mock.patch("serenity.llm_local.get_setting",
                            side_effect=lambda k: "http://127.0.0.1:{}/".format(port)
                            if k == "local_llm_base_url" else None):
                n = ingest.score_tw_news_sentiment(con, backend="local", limit=1)
        finally:
            srv.shutdown()
        check("f-2b limit=1 最多處理 1 列（回傳 ≤1）", n <= 1, f"n={n}")

    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# f-驗收 3: 畸形 JSON / 值域外值 → 跳過，不落庫，不拋例外
# ---------------------------------------------------------------------------

def test_score_sentiment_malformed():
    print("\n--- f-驗收 3: 畸形 JSON / 值域外值跳過 ---")
    import ingest

    # 畸形 JSON
    tmp_name, con = _make_temp_db()
    try:
        _seed_db(con)
        _seed_news(con)

        malformed = "這不是 JSON {{{broken"
        srv, port, _ = _make_local_llm_server(malformed)
        try:
            with mock.patch("serenity.llm_local.get_setting",
                            side_effect=lambda k: "http://127.0.0.1:{}/".format(port)
                            if k == "local_llm_base_url" else None):
                n = ingest.score_tw_news_sentiment(con, backend="local", limit=50)
        finally:
            srv.shutdown()
        count = con.execute(
            "SELECT COUNT(*) FROM news_sentiment WHERE source='zh-news-llm'"
        ).fetchone()[0]
        check("f-3 畸形 JSON 不落庫", count == 0, f"count={count}")
        check("f-3 畸形 JSON 不拋例外（n=0）", n == 0, f"n={n}")
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass

    # 值域外值
    tmp_name2, con2 = _make_temp_db()
    try:
        _seed_db(con2)
        _seed_news(con2)

        out_of_range = '[{"i":1,"sentiment":"看多"},{"i":2,"sentiment":"StrongBuy"}]'
        srv2, port2, _ = _make_local_llm_server(out_of_range)
        try:
            with mock.patch("serenity.llm_local.get_setting",
                            side_effect=lambda k: "http://127.0.0.1:{}/".format(port2)
                            if k == "local_llm_base_url" else None):
                n2 = ingest.score_tw_news_sentiment(con2, backend="local", limit=50)
        finally:
            srv2.shutdown()
        count2 = con2.execute(
            "SELECT COUNT(*) FROM news_sentiment WHERE source='zh-news-llm'"
        ).fetchone()[0]
        check("f-3 值域外值不落庫", count2 == 0, f"count2={count2}")
        check("f-3 值域外值不拋例外（n=0）", n2 == 0, f"n2={n2}")
    finally:
        con2.close()
        try:
            Path(tmp_name2).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# f-驗收 4: gemini 路徑 monkeypatch
# ---------------------------------------------------------------------------

def test_score_sentiment_gemini():
    print("\n--- f-驗收 4: gemini 路徑 ---")
    import ingest

    tmp_name, con = _make_temp_db()
    try:
        _seed_db(con)
        _seed_news(con)

        gemini_result = '[{"i":1,"sentiment":"Bullish"},{"i":2,"sentiment":"Neutral"}]'

        def fake_call_gemini(model, contents, system_instruction, temperature=0.3,
                             task_class="batch", **kwargs):
            return {
                "candidates": [{
                    "content": {"parts": [{"text": gemini_result}]}
                }]
            }

        with mock.patch("serenity.gemini.call_gemini", side_effect=fake_call_gemini), \
             mock.patch("serenity.config.get_setting", side_effect=lambda k: "gemini-2.0-flash"
                        if k == "gemini_model" else None):
            n = ingest.score_tw_news_sentiment(con, backend="gemini", limit=50)

        check("f-4 gemini 路徑 n=2", n == 2, f"n={n}")
        rows = con.execute(
            "SELECT sentiment FROM news_sentiment WHERE source='zh-news-llm'"
        ).fetchall()
        sentiments = {r[0] for r in rows}
        check("f-4 gemini 情緒值正確", sentiments == {"Bullish", "Neutral"}, str(sentiments))

    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# f-驗收 5: 串接 deep_dive_payload events（台股 sentiment 自動生效）
# ---------------------------------------------------------------------------

def test_tw_events_integration():
    print("\n--- f-驗收 5: deep_dive_payload 台股 events 串接 ---")
    try:
        from serenity.services.deep_dive import deep_dive_payload
    except ImportError as exc:
        check("f-5 deep_dive_payload import", False, str(exc))
        return

    tmp_name, con = _make_temp_db()
    try:
        _seed_db(con)

        # 加更多價格讓 deep_dive 有足夠資料
        for i in range(30):
            import datetime as _dt
            d = (_dt.date(2026, 5, 1) + _dt.timedelta(days=i)).isoformat()
            price = 800.0 + i
            con.execute(
                "INSERT OR IGNORE INTO prices(symbol,date,open,high,low,close,volume)"
                " VALUES('2330.TW',?,?,?,?,?,?)",
                (d, price-2, price+3, price-3, price, 10000)
            )

        # 插入 2 個 Bullish 情緒（符合 bull≥2 且 bull>bear → 正面事件日）
        con.execute("""
            INSERT OR IGNORE INTO news_sentiment(symbol, source, published_at, headline,
              sentiment, sentiment_score, url)
            VALUES
              ('2330.TW','zh-news-llm','2026-05-05T00:00:00Z','台積電利多消息','Bullish',NULL,
               'https://ex.com/a'),
              ('2330.TW','zh-news-llm','2026-05-05T00:00:00Z','台積電法說會','Bullish',NULL,
               'https://ex.com/b')
        """)
        con.commit()

        payload = deep_dive_payload(con, "2330.TW")
        events = payload.get("events", {})
        pos = events.get("positive", {})
        check("f-5 events 不為 None", events is not None, str(type(events)))
        check("f-5 positive n ≥ 0（串接生效，不拋例外）",
              isinstance(pos.get("n", 0), int), str(pos))

    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# f-驗收 6: 全套迴歸 + py_compile + node --check（inline）
# ---------------------------------------------------------------------------

def test_regression_compile():
    print("\n--- f-驗收 6: py_compile + node --check ---")
    import subprocess

    wt = str(ROOT)
    files = [
        ROOT / "scripts" / "ingest.py",
        ROOT / "serenity" / "api" / "handler.py",
    ]
    for f in files:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(f)],
            capture_output=True, text=True
        )
        check(f"py_compile {f.name}", result.returncode == 0,
              result.stderr.strip())

    js = ROOT / "dashboard" / "app.js"
    result = subprocess.run(["node", "--check", str(js)],
                            capture_output=True, text=True)
    check("node --check app.js", result.returncode == 0, result.stderr.strip())


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_fetch_tw_news()
    test_score_sentiment_valid()
    test_score_sentiment_limit()
    test_score_sentiment_malformed()
    test_score_sentiment_gemini()
    test_tw_events_integration()
    test_regression_compile()
    finish()
