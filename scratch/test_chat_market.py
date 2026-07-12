# -*- coding: utf-8 -*-
"""
(a) 聊天市場總覽 context — 驗收測試(規格:docs/REQUIREMENTS_AI_MARKET.md a-驗收)

執行:PYTHONIOENCODING=utf-8 python scratch/test_chat_market.py
原則:只用 data/serenity.sqlite 的 tempfile 副本;不打真 Gemini(只測 builder)。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok))
    mark = "  OK " if ok else "! FAIL"
    print(f"{mark}  {name}" + (f" -- {detail}" if (detail and not ok) else ""))


def finish():
    passed = sum(1 for _, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("=" * 70)
    print(f"Chat Market Overview Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    return 1 if failed else 0


def main():
    from serenity.services.chat import build_market_overview, _detect_market_intent

    # ── sandbox DB(tempfile 副本,正式 DB 只讀)──────────────────────────
    tmpdir = Path(tempfile.mkdtemp(prefix="chat_market_test_"))
    test_db = tmpdir / "test.sqlite"
    src = sqlite3.connect(ROOT / "data" / "serenity.sqlite")
    dst = sqlite3.connect(test_db)
    src.backup(dst)
    src.close()
    dst.close()
    con = sqlite3.connect(test_db)
    con.row_factory = sqlite3.Row

    # 1. 快照含 prices 最大 date
    as_of = con.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    compact = build_market_overview(con, extended=False)
    check("快照非空", bool(compact))
    check("快照含 prices 最大 date", as_of in compact, f"as_of={as_of}")

    # 2. 漲幅第一名與 SQL 現算一致(symbol 與 % 字串,2 位小數)
    rows = con.execute(
        """
        WITH ranked AS (
          SELECT symbol, close,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
          FROM prices
        )
        SELECT a.symbol, a.close AS c0, b.close AS c1
        FROM ranked a JOIN ranked b ON a.symbol=b.symbol AND a.rn=1 AND b.rn=2
        WHERE b.close != 0
        """
    ).fetchall()
    calc = [(r["symbol"], (r["c0"] / r["c1"] - 1) * 100) for r in rows]
    top_sym, top_chg = max(calc, key=lambda t: t[1])
    top_str = f"{'+' if top_chg >= 0 else ''}{round(top_chg, 4):.2f}%"
    check("漲幅第一名 symbol 在快照中", top_sym in compact, f"top={top_sym}")
    check("漲幅第一名 % 字串與 SQL 現算一致", f"{top_sym} {top_str}" in compact,
          f"expected '{top_sym} {top_str}'")

    # 3. 觀察清單:有值 → symbol 出現;清空 → 顯示 (空)
    con.execute("INSERT OR IGNORE INTO watchlist(symbol, added_at) VALUES ('NVDA', '2026-01-01T00:00:00Z')")
    con.commit()
    with_wl = build_market_overview(con, extended=False)
    wl_line = [l for l in with_wl.splitlines() if l.startswith("- 觀察清單")]
    check("觀察清單有值時 symbol 出現在快照", wl_line and "NVDA" in wl_line[0],
          f"line={wl_line}")
    con.execute("DELETE FROM watchlist")
    con.commit()
    no_wl = build_market_overview(con, extended=False)
    check("觀察清單清空時顯示 (空)", "- 觀察清單：(空)" in no_wl)

    # 4. 市場意圖偵測
    for msg, expect in [
        ("大盤現在怎麼看", True), ("哪個類股最強", True),
        ("market outlook?", True), ("分析 NVDA", False), ("TSLA 財報", False),
    ]:
        check(f"_detect_market_intent({msg!r}) == {expect}",
              _detect_market_intent(msg) == expect)

    # 5. extended 嚴格多於 compact
    extended = build_market_overview(con, extended=True)
    check("extended 長度 > compact", len(extended) > len(compact),
          f"{len(extended)} vs {len(compact)}")
    check("extended 含 5日% 欄", "5日%" in extended and "5日%" not in compact)
    check("extended 含成交量前 5", "成交量前 5" in extended and "成交量前 5" not in compact)
    check("extended 漲幅列出前 10", "當日漲幅前 10" in extended)

    # 6. 不含 "None";regime 行存在(bull/bear/neutral/unknown 其一)
    for name, s in [("compact", compact), ("extended", extended)]:
        check(f"{name} 不含 'None'", "None" not in s)
    check("regime 行存在且值合法",
          any(f"市場狀態：{v}" in compact for v in ("bull", "bear", "neutral", "unknown")))

    con.close()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
