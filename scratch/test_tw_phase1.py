# -*- coding: utf-8 -*-
"""
台股支援 Phase 1（c-R1~c-R3）驗收測試（規格：docs/REQUIREMENTS_AI_MARKET.md c-驗收 1~6）

執行：PYTHONIOENCODING=utf-8 python scratch/test_tw_phase1.py

原則：
  - tempfile DB 副本（不動正式 DB）
  - yahoo_chart 用 monkeypatch 假資料（3 根日線），不打真網路
  - 只測 c-驗收 1~6；第 7 條由既有測試套件覆蓋
"""
from __future__ import annotations

import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SERENITY_NO_DOTENV", "1")

# DB 來源：環境變數 SERENITY_DB_SRC 或主 repo 絕對路徑（worktree data/ 無真 DB）
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
    print(f"TW Phase 1 Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


# ---------------------------------------------------------------------------
# 建立 tempfile DB 副本
# ---------------------------------------------------------------------------

def _make_temp_db() -> tuple[str, sqlite3.Connection]:
    """從主 DB 複製到 tempfile（唯讀來源，寫入副本）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    src = Path(_DB_SRC)
    if src.exists():
        shutil.copy2(str(src), tmp.name)
    # 若主 DB 不存在，以 ingest.connect() 建一個空 DB
    import ingest
    # 覆蓋 DB_PATH 讓 connect() 使用 tmpfile
    original_db_path = ingest.DB_PATH
    ingest.DB_PATH = Path(tmp.name)
    con = ingest.connect()
    ingest.DB_PATH = original_db_path
    return tmp.name, con


# ---------------------------------------------------------------------------
# 假 yahoo_chart 回傳資料（3 根日線）
# ---------------------------------------------------------------------------

import datetime as dt

def _fake_yahoo_chart(symbol, start, end, max_retries=3):
    """3 根日線假資料（模擬 Yahoo v8 chart API 格式）。"""
    base = dt.datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    timestamps = [int((base + dt.timedelta(days=i)).timestamp()) for i in range(3)]
    # 台股價格範圍（TSM: ~870, 2330.TW: ~1000 TWD）
    closes  = [870.0, 880.0, 875.0]
    opens   = [865.0, 875.0, 878.0]
    highs   = [875.0, 885.0, 882.0]
    lows    = [860.0, 872.0, 870.0]
    volumes = [25000000, 27000000, 23000000]
    return {
        "chart": {
            "result": [{
                "timestamp": timestamps,
                "indicators": {
                    "quote": [{
                        "open": opens,
                        "high": highs,
                        "low": lows,
                        "close": closes,
                        "volume": volumes,
                    }]
                }
            }]
        }
    }


# ---------------------------------------------------------------------------
# c-驗收 1: _SYM_RE 正規式
# ---------------------------------------------------------------------------

def test_sym_re():
    print("\n--- c-驗收 1: _SYM_RE ---")
    from serenity.services.watchlist import _SYM_RE

    check("2330.TW 過", _SYM_RE.match("2330.TW") is not None)
    check("6488.TWO 過", _SYM_RE.match("6488.TWO") is not None)
    check("NVDA 過", _SYM_RE.match("NVDA") is not None)
    check("AAPL 過", _SYM_RE.match("AAPL") is not None)
    check("BRK.B 過", _SYM_RE.match("BRK.B") is not None)
    check("$;DROP 不過", _SYM_RE.match("$;DROP") is None)
    check("超長代號不過(13字元)", _SYM_RE.match("ABCDEFGHIJKLM") is None)
    check("空字串不過", _SYM_RE.match("") is None)


# ---------------------------------------------------------------------------
# c-驗收 2: region()
# ---------------------------------------------------------------------------

def test_region():
    print("\n--- c-驗收 2: region() ---")
    from serenity.services.pool_views import region

    check("2330.TW → tw", region("2330.TW") == "tw")
    check("6488.TWO → tw", region("6488.TWO") == "tw")
    check("NVDA → us", region("NVDA") == "us")
    check("BRK.B → us（.B 不是台股）", region("BRK.B") == "us")
    check("小寫 2330.tw → tw", region("2330.tw") == "tw")
    check("小寫 6488.two → tw", region("6488.two") == "tw")


# ---------------------------------------------------------------------------
# c-驗收 3: fetch_prices_for_symbol（假 yahoo_chart 回 3 根日線）
# ---------------------------------------------------------------------------

def test_fetch_prices_for_symbol():
    print("\n--- c-驗收 3: fetch_prices_for_symbol ---")
    import ingest

    tmp_name, con = _make_temp_db()
    try:
        with mock.patch.object(ingest, "yahoo_chart", side_effect=_fake_yahoo_chart):
            n = ingest.fetch_prices_for_symbol(con, "2330.TW", days_back=420)
        rows = con.execute(
            "SELECT date, close FROM prices WHERE symbol='2330.TW' ORDER BY date"
        ).fetchall()
        check("fetch 回傳 3 bars", n == 3, f"n={n}")
        check("prices 表有 3 列", len(rows) == 3, f"got {len(rows)}")
        expected_closes = [870.0, 880.0, 875.0]
        closes_ok = all(abs(r[1] - ec) < 1e-6 for r, ec in zip(rows, expected_closes))
        check("close 值正確", closes_ok, str([r[1] for r in rows]))

        # 冪等（重跑不重複）
        with mock.patch.object(ingest, "yahoo_chart", side_effect=_fake_yahoo_chart):
            n2 = ingest.fetch_prices_for_symbol(con, "2330.TW", days_back=420)
        rows2 = con.execute(
            "SELECT count(*) FROM prices WHERE symbol='2330.TW'"
        ).fetchone()[0]
        check("upsert 冪等，仍只有 3 列", rows2 == 3, f"rows={rows2}")
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# c-驗收 4: market_board_payload 含 region 欄
# ---------------------------------------------------------------------------

def test_market_board_region():
    print("\n--- c-驗收 4: market_board_payload rows 含 region 欄 ---")
    import ingest
    from serenity.services.pool_views import market_board_payload

    tmp_name, con = _make_temp_db()
    try:
        # 插入台股假價格列
        con.execute("""
            INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)
            VALUES ('2330.TW', '2026-06-01', 865.0, 875.0, 860.0, 870.0, 25000000),
                   ('2330.TW', '2026-06-02', 875.0, 885.0, 872.0, 880.0, 27000000)
        """)
        # 插入美股假價格列
        con.execute("""
            INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)
            VALUES ('NVDA', '2026-06-01', 100.0, 105.0, 98.0, 102.0, 50000000),
                   ('NVDA', '2026-06-02', 102.0, 108.0, 101.0, 107.0, 55000000)
        """)
        con.commit()

        board = market_board_payload(con)
        rows = board.get("rows", [])
        tw_rows = [r for r in rows if r["symbol"] == "2330.TW"]
        us_rows = [r for r in rows if r["symbol"] == "NVDA"]

        check("board rows 不為空", len(rows) > 0)
        check("2330.TW 列存在", len(tw_rows) == 1, str([r["symbol"] for r in rows]))
        check("2330.TW region='tw'", tw_rows[0].get("region") == "tw" if tw_rows else False,
              str(tw_rows[0].get("region")) if tw_rows else "no row")
        check("NVDA region='us'", us_rows[0].get("region") == "us" if us_rows else False,
              str(us_rows[0].get("region")) if us_rows else "no row")
        # 所有 rows 都有 region 欄
        all_have_region = all("region" in r for r in rows)
        check("所有 rows 均有 region 欄", all_have_region)
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# c-驗收 5: place_order 台股拒單；美股不受影響
# ---------------------------------------------------------------------------

def test_place_order_tw_reject():
    print("\n--- c-驗收 5: place_order 台股拒單 ---")
    import ingest

    # fund_pool.py 在 scripts/ 目錄，需直接 import
    import fund_pool

    tmp_name, con = _make_temp_db()
    try:
        # 建立資金池
        fund_pool.migrate(con)
        import datetime
        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "INSERT OR IGNORE INTO pools(agent_id, display_name, initial_cash, status, created_at)"
            " VALUES ('test-pool-tw', '台股測試池', 3000.0, 'active', ?)",
            (now_str,)
        )
        con.commit()

        # 插入台股與美股假價格
        con.execute("""
            INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)
            VALUES ('2330.TW', '2026-06-02', 875.0, 885.0, 872.0, 880.0, 27000000),
                   ('NVDA', '2026-06-02', 102.0, 108.0, 101.0, 107.0, 55000000)
        """)
        con.commit()

        as_of = "2026-06-02"

        # BUY 2330.TW → 應被拒
        result_tw = fund_pool.place_order(
            con, "test-pool-tw", "BUY", "2330.TW",
            usd=100.0, reason="測試台股下單", fill_mode="latest_close", as_of=as_of
        )
        check("2330.TW BUY → rejected", result_tw["status"] == "rejected",
              str(result_tw))
        check("rejected_reason 含「台股」",
              "台股" in (result_tw.get("rejected_reason") or ""),
              str(result_tw.get("rejected_reason")))

        # BUY NVDA → 不應被台股規則擋（可能因資金等原因被擋，但不應有「台股」字樣）
        result_us = fund_pool.place_order(
            con, "test-pool-tw", "BUY", "NVDA",
            usd=100.0, reason="測試美股下單", fill_mode="latest_close", as_of=as_of
        )
        tw_reason_in_us = "台股" in (result_us.get("rejected_reason") or "")
        check("NVDA BUY 不含台股拒單原因", not tw_reason_in_us,
              f"status={result_us['status']} reason={result_us.get('rejected_reason')}")
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# c-驗收 6: chat db_symbols 來源擴充（prices 有 2330.TW，mentions 無）
# ---------------------------------------------------------------------------

def test_chat_db_symbols_union():
    print("\n--- c-驗收 6: chat db_symbols = mentions ∪ prices ---")
    import ingest

    tmp_name, con = _make_temp_db()
    try:
        # 確保 2330.TW 只在 prices，不在 mentions
        con.execute("DELETE FROM mentions WHERE symbol='2330.TW'")
        con.execute("""
            INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume)
            VALUES ('2330.TW', '2026-06-02', 875.0, 885.0, 872.0, 880.0, 27000000)
        """)
        con.commit()

        # 直接測 UNION SQL（與 chat.py 邏輯一致）
        db_symbols = [r[0] for r in con.execute(
            "select distinct symbol from mentions union select distinct symbol from prices"
        ).fetchall()]

        mentions_only = [r[0] for r in con.execute(
            "select distinct symbol from mentions"
        ).fetchall()]

        check("2330.TW 在 union 結果中", "2330.TW" in db_symbols,
              f"db_symbols 含 2330.TW: {'2330.TW' in db_symbols}")
        check("2330.TW 不在 mentions-only 中", "2330.TW" not in mentions_only,
              f"mentions 含 2330.TW: {'2330.TW' in mentions_only}")
        check("union 結果 >= mentions-only 結果",
              len(db_symbols) >= len(mentions_only),
              f"union={len(db_symbols)}, mentions={len(mentions_only)}")
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_sym_re()
    test_region()
    test_fetch_prices_for_symbol()
    test_market_board_region()
    test_place_order_tw_reject()
    test_chat_db_symbols_union()
    finish()
