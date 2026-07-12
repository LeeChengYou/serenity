# -*- coding: utf-8 -*-
"""
(c-2) 台股全目錄 + 按需抓價 驗收測試（規格：docs/REQUIREMENTS_AI_MARKET.md c2-驗收 1-6）

執行：PYTHONIOENCODING=utf-8 python scratch/test_tw_directory.py
通過標準：0 failed、exit 0。
原則：
  - 假 HTTP server fixture（threading + http.server），不打真網路
  - tempfile DB 副本
  - monkeypatch fetch_prices_for_symbol 計數（tw-seed 不打真網路）
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
    print(f"TW Directory Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


# ---------------------------------------------------------------------------
# tempfile DB 工廠（不依賴主 DB；從空 DB 建）
# ---------------------------------------------------------------------------

def _make_temp_db() -> tuple[str, sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    src = Path(_DB_SRC)
    if src.exists():
        shutil.copy2(str(src), tmp.name)
    import ingest
    original_db_path = ingest.DB_PATH
    ingest.DB_PATH = Path(tmp.name)
    con = ingest.connect()
    ingest.migrate_tw_symbols(con)
    ingest.DB_PATH = original_db_path
    return tmp.name, con


# ---------------------------------------------------------------------------
# 假 HTTP server 工廠
# ---------------------------------------------------------------------------

def _make_fixture_server(twse_items=None, tpex_items=None,
                         twse_status=200, tpex_status=200) -> tuple:
    """
    啟動本地假 HTTP server。
    - GET /twse → twse_items（JSON）；狀態碼 twse_status
    - GET /tpex → tpex_items（JSON）；狀態碼 tpex_status
    回傳 (server, port, thread)。
    """
    _twse = json.dumps(twse_items or []).encode("utf-8")
    _tpex = json.dumps(tpex_items or []).encode("utf-8")
    _twse_st = twse_status
    _tpex_st = tpex_status

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/twse":
                body, status = _twse, _twse_st
            elif self.path == "/tpex":
                body, status = _tpex, _tpex_st
            else:
                body, status = b"not found", 404
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever)
    t.daemon = True
    t.start()
    return srv, port, t


# ---------------------------------------------------------------------------
# c2-驗收 1: fetch_tw_directory fixture 3+3 → 6 列，冪等，畸形跳過
# ---------------------------------------------------------------------------

def test_fetch_tw_directory_basic():
    print("\n--- c2-驗收 1: fetch_tw_directory 基本 ---")
    import ingest

    twse_items = [
        {"公司代號": "2330", "公司簡稱": "台積電"},
        {"公司代號": "2317", "公司簡稱": "鴻海"},
        {"公司代號": "BADCODE!", "公司簡稱": "畸形代號"},  # 非數字開頭 → 應被跳過
    ]
    tpex_items = [
        {"SecuritiesCompanyCode": "6488", "CompanyAbbreviation": "環球晶"},
        {"SecuritiesCompanyCode": "3231", "CompanyAbbreviation": "緯創"},
        {"SecuritiesCompanyCode": "12345678",  "CompanyAbbreviation": "超長碼"},  # 8 碼 → 超出 ^\d{4,6}[A-Z]?$ 應被跳過
    ]

    srv, port, _ = _make_fixture_server(twse_items, tpex_items)
    tmp_name, con = _make_temp_db()
    try:
        with mock.patch.object(ingest, "_TWSE_URL", f"http://127.0.0.1:{port}/twse"), \
             mock.patch.object(ingest, "_TPEX_URL", f"http://127.0.0.1:{port}/tpex"):
            twse_n, tpex_n = ingest.fetch_tw_directory(con)

        rows = con.execute("SELECT code, market, yahoo_symbol FROM tw_symbols ORDER BY code").fetchall()
        codes = [r[0] for r in rows]
        markets = {r[0]: r[1] for r in rows}
        symbols = {r[0]: r[2] for r in rows}

        check("TWSE 有效 2 列", twse_n == 2, f"twse_n={twse_n}")
        check("TPEX 有效 2 列", tpex_n == 2, f"tpex_n={tpex_n}")
        check("tw_symbols 共 4 列（畸形被跳過）", len(rows) == 4, str(codes))
        check("2330 market=twse", markets.get("2330") == "twse", str(markets))
        check("6488 market=tpex", markets.get("6488") == "tpex", str(markets))
        check("2330 yahoo_symbol=2330.TW", symbols.get("2330") == "2330.TW", str(symbols))
        check("6488 yahoo_symbol=6488.TWO", symbols.get("6488") == "6488.TWO", str(symbols))
        check("畸形 BADCODE! 不在表", "BADCODE!" not in codes, str(codes))
        check("超長 12345678 不在表", "12345678" not in codes, str(codes))

        # 冪等：重跑不重複
        with mock.patch.object(ingest, "_TWSE_URL", f"http://127.0.0.1:{port}/twse"), \
             mock.patch.object(ingest, "_TPEX_URL", f"http://127.0.0.1:{port}/tpex"):
            ingest.fetch_tw_directory(con)
        rows2 = con.execute("SELECT COUNT(*) FROM tw_symbols").fetchone()[0]
        check("重跑冪等，仍 4 列", rows2 == 4, f"rows={rows2}")
    finally:
        srv.shutdown()
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# c2-驗收 2: 單端 500 → 另一端正常；舊列保留；兩端皆失敗 → raise
# ---------------------------------------------------------------------------

def test_fetch_tw_directory_failure():
    print("\n--- c2-驗收 2: 單端失敗保留舊列；兩端皆失敗 raise ---")
    import ingest

    twse_items = [{"公司代號": "2330", "公司簡稱": "台積電"}]
    tpex_items = [{"SecuritiesCompanyCode": "6488", "CompanyAbbreviation": "環球晶"}]

    # ── 2a: 先插入 TWSE 舊列，再讓 TWSE 失敗 ────────────────────────────────
    srv, port, _ = _make_fixture_server(twse_items, tpex_items,
                                        twse_status=500, tpex_status=200)
    tmp_name, con = _make_temp_db()
    try:
        # 先插入假舊列（模擬上次成功）
        con.execute(
            "INSERT OR REPLACE INTO tw_symbols(code, name, market, yahoo_symbol, updated_at)"
            " VALUES('2330', '台積電舊', 'twse', '2330.TW', '2026-01-01T00:00:00Z')")
        con.commit()

        with mock.patch.object(ingest, "_TWSE_URL", f"http://127.0.0.1:{port}/twse"), \
             mock.patch.object(ingest, "_TPEX_URL", f"http://127.0.0.1:{port}/tpex"):
            twse_n, tpex_n = ingest.fetch_tw_directory(con)

        rows = con.execute("SELECT code FROM tw_symbols ORDER BY code").fetchall()
        codes = [r[0] for r in rows]
        check("2a TWSE 500 時不 raise", True)  # 到這裡就代表沒 raise
        check("2a TPEX 正常更新 1 列", tpex_n == 1, f"tpex_n={tpex_n}")
        check("2a TWSE 舊列 2330 仍在", "2330" in codes, str(codes))
        check("2a TPEX 6488 也在", "6488" in codes, str(codes))
    except Exception as exc:
        check("2a 單端 500 不應 raise", False, str(exc))
    finally:
        srv.shutdown()
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass

    # ── 2b: 兩端皆失敗 → raise ──────────────────────────────────────────────
    srv2, port2, _ = _make_fixture_server(twse_status=500, tpex_status=500)
    tmp_name2, con2 = _make_temp_db()
    raised = False
    try:
        with mock.patch.object(ingest, "_TWSE_URL", f"http://127.0.0.1:{port2}/twse"), \
             mock.patch.object(ingest, "_TPEX_URL", f"http://127.0.0.1:{port2}/tpex"):
            ingest.fetch_tw_directory(con2)
    except RuntimeError:
        raised = True
    except Exception:
        raised = True
    finally:
        srv2.shutdown()
        con2.close()
        try:
            Path(tmp_name2).unlink()
        except Exception:
            pass
    check("2b 兩端皆失敗 → raise", raised)


# ---------------------------------------------------------------------------
# c2-驗收 3: /api/tw/search  — 400 / directory_empty / 前綴 / 子字串 / limit 20
# ---------------------------------------------------------------------------

def test_tw_search_api():
    print("\n--- c2-驗收 3: /api/tw/search ---")
    from serenity.api.handler import Handler, _HTTPResponse
    import serenity.api.handler as hmod

    tmp_name, con = _make_temp_db()
    try:
        # 先插入 tw_symbols 資料
        sym_rows = [
            ("2330", "台積電", "twse", "2330.TW"),
            ("2317", "鴻海精密", "twse", "2317.TW"),
            ("6488", "環球晶圓", "tpex", "6488.TWO"),
        ]
        for code, name, market, yahoo in sym_rows:
            con.execute(
                "INSERT OR REPLACE INTO tw_symbols(code, name, market, yahoo_symbol, updated_at)"
                " VALUES(?, ?, ?, ?, '2026-01-01T00:00:00Z')",
                (code, name, market, yahoo)
            )
        # 2330.TW 有價格；其他沒有
        con.execute(
            "INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)"
            " VALUES('2330.TW', '2026-06-01', 800.0, 820.0, 790.0, 810.0, 10000)"
        )
        con.commit()
        con.close()  # 讓後續的 fresh_con 可以讀到我們寫入的資料

        h = Handler.__new__(Handler)

        def _route(path, q_str=""):
            """每次 open a fresh connection（route_api 會 close 它）"""
            from urllib.parse import parse_qs
            qd = parse_qs(q_str)
            fresh = sqlite3.connect(tmp_name)
            fresh.row_factory = sqlite3.Row
            with mock.patch.object(hmod, "DB_PATH", Path(tmp_name)), \
                 mock.patch.object(hmod, "db", return_value=fresh):
                return h.route_api(path, qd)

        # 400: q 空
        raised_400 = False
        try:
            _route("/api/tw/search", "q=")
        except _HTTPResponse as exc:
            raised_400 = exc.status == 400
        check("q 空 → 400", raised_400)

        # directory_empty: 用空記憶體 DB
        empty_tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        empty_tmp.close()
        empty_con_setup = sqlite3.connect(empty_tmp.name)
        empty_con_setup.execute("""create table if not exists tw_symbols (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL,
            yahoo_symbol TEXT NOT NULL UNIQUE, updated_at TEXT NOT NULL)""")
        empty_con_setup.execute("""create table if not exists prices (
            symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL NOT NULL, volume INTEGER, PRIMARY KEY(symbol,date))""")
        empty_con_setup.commit()
        empty_con_setup.close()

        def _route_empty(path, q_str=""):
            from urllib.parse import parse_qs
            qd = parse_qs(q_str)
            fresh = sqlite3.connect(empty_tmp.name)
            fresh.row_factory = sqlite3.Row
            with mock.patch.object(hmod, "DB_PATH", Path(empty_tmp.name)), \
                 mock.patch.object(hmod, "db", return_value=fresh):
                return h.route_api(path, qd)

        res = _route_empty("/api/tw/search", "q=台")
        check("空目錄 directory_empty=true", res.get("directory_empty") is True, str(res))
        try:
            Path(empty_tmp.name).unlink()
        except Exception:
            pass

        # 代號前綴命中
        res = _route("/api/tw/search", "q=233")
        items = res.get("items", [])
        codes = [i["code"] for i in items]
        check("代號前綴 '233' 命中 2330", "2330" in codes, str(codes))

        # name 子字串命中
        res = _route("/api/tw/search", "q=環球")
        items = res.get("items", [])
        codes = [i["code"] for i in items]
        check("name 子字串 '環球' 命中 6488", "6488" in codes, str(codes))

        # limit 20（插入 25 筆，驗回傳 ≤20）
        setup_con = sqlite3.connect(tmp_name)
        for i in range(25):
            setup_con.execute(
                "INSERT OR IGNORE INTO tw_symbols(code, name, market, yahoo_symbol, updated_at)"
                " VALUES(?, ?, 'twse', ?, '2026-01-01T00:00:00Z')",
                (f"9{i:03d}", f"測試公司{i}", f"9{i:03d}.TW")
            )
        setup_con.commit()
        setup_con.close()
        res = _route("/api/tw/search", "q=測試")
        check("limit 20 不超限", len(res.get("items", [])) <= 20,
              f"got {len(res.get('items', []))}")

        # has_prices 欄位正確
        res = _route("/api/tw/search", "q=2330")
        items = res.get("items", [])
        tw_item = next((i for i in items if i["code"] == "2330"), None)
        check("2330 has_prices=true", tw_item is not None and tw_item.get("has_prices") is True,
              str(tw_item))
    finally:
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# c2-驗收 4: market_board_payload 台股列帶 name
# ---------------------------------------------------------------------------

def test_market_board_name():
    print("\n--- c2-驗收 4: market_board_payload 台股列帶 name ---")
    from serenity.services.pool_views import market_board_payload

    tmp_name, con = _make_temp_db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO tw_symbols(code, name, market, yahoo_symbol, updated_at)"
            " VALUES('2330', '台積電', 'twse', '2330.TW', '2026-01-01T00:00:00Z')"
        )
        con.execute("""
            INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)
            VALUES ('2330.TW', '2026-06-01', 865.0, 875.0, 860.0, 870.0, 25000000),
                   ('2330.TW', '2026-06-02', 875.0, 885.0, 872.0, 880.0, 27000000)
        """)
        con.execute("""
            INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)
            VALUES ('NVDA', '2026-06-01', 100.0, 105.0, 98.0, 102.0, 50000000),
                   ('NVDA', '2026-06-02', 102.0, 108.0, 101.0, 107.0, 55000000)
        """)
        con.commit()

        board = market_board_payload(con)
        rows = board.get("rows", [])
        tw_rows = [r for r in rows if r.get("symbol") == "2330.TW"]
        us_rows = [r for r in rows if r.get("symbol") == "NVDA"]

        check("2330.TW 列存在", len(tw_rows) == 1, str([r["symbol"] for r in rows]))
        check("2330.TW 帶 name='台積電'",
              tw_rows[0].get("name") == "台積電" if tw_rows else False,
              str(tw_rows[0] if tw_rows else {}))
        check("NVDA 列存在", len(us_rows) == 1)
        # NVDA 沒有 tw_symbols 列，name 應為 None 或不存在（不強制要求）
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# c2-驗收 5: tw-seed — 跳過/呼叫行為（monkeypatch fetch_prices_for_symbol）
# ---------------------------------------------------------------------------

def test_tw_seed_behavior():
    print("\n--- c2-驗收 5: tw-seed 行為 ---")
    import ingest

    tmp_name, con = _make_temp_db()
    try:
        # 清掉真實 DB 中可能已有的台股價格（副本可能含真實資料）
        con.execute("DELETE FROM prices WHERE symbol IN ('2330.TW', '2317.TW')")
        # 清掉 tw_symbols（如有殘留）
        con.execute("DELETE FROM tw_symbols WHERE code IN ('2330', '2317')")
        con.commit()

        # 插入 2330 與 6488 到 tw_symbols；插入 2317（有價格）
        con.execute(
            "INSERT OR REPLACE INTO tw_symbols(code, name, market, yahoo_symbol, updated_at)"
            " VALUES('2330', '台積電', 'twse', '2330.TW', '2026-01-01T00:00:00Z')"
        )
        con.execute(
            "INSERT OR REPLACE INTO tw_symbols(code, name, market, yahoo_symbol, updated_at)"
            " VALUES('2317', '鴻海', 'twse', '2317.TW', '2026-01-01T00:00:00Z')"
        )
        # 2317.TW 已有價格
        con.execute(
            "INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)"
            " VALUES('2317.TW', '2026-06-01', 100.0, 105.0, 98.0, 102.0, 1000000)"
        )
        con.commit()

        call_log = []

        def fake_fetch_prices(c, sym, days_back=420):
            call_log.append(sym)
            return 3

        # 使用只含三個代號的種子（2330 在目錄、2317 在目錄但有價格、9999 不在目錄）
        seed_codes = ["2330", "2317", "9999"]
        with mock.patch.object(ingest, "_load_tw_seed_codes", return_value=seed_codes), \
             mock.patch.object(ingest, "fetch_prices_for_symbol", side_effect=fake_fetch_prices), \
             mock.patch.object(ingest, "DB_PATH", Path(tmp_name)):
            ingest.fetch_tw_seed(con)

        check("9999 不在目錄 → 跳過（不呼叫 fetch）", "9999.TW" not in call_log,
              f"call_log={call_log}")
        check("2317.TW 已有價格 → 跳過（不呼叫 fetch）", "2317.TW" not in call_log,
              f"call_log={call_log}")
        check("2330.TW 無價格 → 呼叫 fetch", "2330.TW" in call_log,
              f"call_log={call_log}")
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 額外驗收：種子清單從 data/tw_seed_symbols.txt 讀取
# ---------------------------------------------------------------------------

def test_tw_seed_reads_txt():
    print("\n--- 額外驗收: seed 清單從 tw_seed_symbols.txt 讀取 ---")
    import ingest

    # 建一個臨時 txt 覆蓋
    tmp_txt = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                          delete=False, encoding="utf-8")
    tmp_txt.write("# 2026-07 快照\n2330\n2317\n\n# 注解行\n6488\n")
    tmp_txt.close()

    try:
        with mock.patch.object(ingest, "TW_SEED_FILE", Path(tmp_txt.name)):
            codes = ingest._load_tw_seed_codes()
        check("讀到 3 個代號", len(codes) == 3, str(codes))
        check("包含 2330", "2330" in codes, str(codes))
        check("包含 6488", "6488" in codes, str(codes))
        check("# 行與空行被跳過", len(codes) == 3)
    finally:
        try:
            Path(tmp_txt.name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_fetch_tw_directory_basic()
    test_fetch_tw_directory_failure()
    test_tw_search_api()
    test_market_board_name()
    test_tw_seed_behavior()
    test_tw_seed_reads_txt()
    finish()
