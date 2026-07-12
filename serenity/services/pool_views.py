"""
serenity/services/pool_views.py
資金池 view 層：pool_list_payload / pool_detail_payload / pool_consults_payload
"""
from __future__ import annotations

import sqlite3


def region(symbol: str) -> str:
    """
    判斷代號所屬地區(c-R2)。
    .TW / .TWO 結尾 → 'tw'；其餘 → 'us'。
    """
    sym_upper = (symbol or "").upper()
    if sym_upper.endswith(".TW") or sym_upper.endswith(".TWO"):
        return "tw"
    return "us"


def pool_list_payload(con: sqlite3.Connection) -> dict:
    """
    GET /api/pools
    回傳所有資金池的概要列表。
    字段：pool_id, name, initial_cash, status, nav, cash,
          total_return_pct, mdd, pending_orders, created_at
    """
    pools = con.execute(
        "SELECT agent_id, display_name, initial_cash, status, created_at FROM pools ORDER BY created_at"
    ).fetchall()

    result = []
    for p in pools:
        pool_id = p["agent_id"]

        # 最新 NAV
        nav_row = con.execute(
            "SELECT nav, cash FROM agent_nav_daily WHERE agent_id=? ORDER BY date DESC LIMIT 1",
            (pool_id,)
        ).fetchone()
        nav = nav_row["nav"] if nav_row else p["initial_cash"]
        cash = nav_row["cash"] if nav_row else p["initial_cash"]

        # 若沒有 NAV 記錄，從 agent_state 取 cash
        if nav_row is None:
            state_row = con.execute(
                "SELECT cash FROM agent_state WHERE agent_id=? ORDER BY month DESC LIMIT 1",
                (pool_id,)
            ).fetchone()
            if state_row:
                cash = state_row["cash"]
                nav = cash  # 還沒有持倉記錄時，NAV = cash

        # 總報酬率
        initial = p["initial_cash"]
        total_return_pct = (nav / initial - 1.0) * 100.0 if initial else 0.0

        # MDD（從歷史 NAV 序列計算）
        nav_series = [r["nav"] for r in con.execute(
            "SELECT nav FROM agent_nav_daily WHERE agent_id=? ORDER BY date",
            (pool_id,)
        ).fetchall()]
        mdd = _calc_mdd(nav_series)

        # 未成交單數
        pending_orders = con.execute(
            "SELECT COUNT(*) FROM agent_trades WHERE agent_id=? AND status='pending'",
            (pool_id,)
        ).fetchone()[0]

        result.append({
            "pool_id":          pool_id,
            "name":             p["display_name"],
            "initial_cash":     initial,
            "status":           p["status"],
            "nav":              nav,
            "cash":             cash,
            "total_return_pct": round(total_return_pct, 4),
            "mdd":              round(mdd, 4),
            "pending_orders":   pending_orders,
            "created_at":       p["created_at"],
        })

    return {"pools": result}


def pool_detail_payload(con: sqlite3.Connection, pool_id: str) -> dict:
    """
    GET /api/pools/{id}
    回傳資金池詳情：持倉、NAV 序列、交易紀錄。
    """
    # 持倉
    positions_raw = con.execute(
        "SELECT symbol, qty, avg_cost FROM agent_positions WHERE agent_id=?",
        (pool_id,)
    ).fetchall()
    nav_row = con.execute(
        "SELECT nav FROM agent_nav_daily WHERE agent_id=? ORDER BY date DESC LIMIT 1",
        (pool_id,)
    ).fetchone()
    total_nav = nav_row["nav"] if nav_row else None

    positions = []
    for pos in positions_raw:
        sym = pos["symbol"]
        lc_row = con.execute(
            "SELECT close, date FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (sym,)
        ).fetchone()
        last_close = lc_row["close"] if lc_row else None
        unrealized_pnl = None
        weight_pct = None
        if last_close is not None:
            pos_value = pos["qty"] * last_close
            unrealized_pnl = pos_value - pos["qty"] * pos["avg_cost"]
            if total_nav and total_nav > 0:
                weight_pct = pos_value / total_nav * 100.0
        positions.append({
            "symbol":         sym,
            "qty":            pos["qty"],
            "avg_cost":       pos["avg_cost"],
            "last_close":     last_close,
            "unrealized_pnl": unrealized_pnl,
            "weight_pct":     weight_pct,
        })

    # NAV 序列
    nav_series = [
        {"date": r["date"], "nav": r["nav"]}
        for r in con.execute(
            "SELECT date, nav FROM agent_nav_daily WHERE agent_id=? ORDER BY date",
            (pool_id,)
        ).fetchall()
    ]

    # 交易紀錄（含 fill_mode / reason / status）
    trades_raw = con.execute(
        "SELECT decided_date, exec_date, symbol, side, qty, price, usd, "
        "       reason, status, rejected_reason, fill_mode "
        "FROM agent_trades WHERE agent_id=? ORDER BY id DESC",
        (pool_id,)
    ).fetchall()
    trades = []
    for t in trades_raw:
        trades.append({
            "decided_date":    t["decided_date"],
            "exec_date":       t["exec_date"],
            "symbol":          t["symbol"],
            "side":            t["side"],
            "qty":             t["qty"],
            "price":           t["price"],
            "usd":             t["usd"],
            "reason":          t["reason"],
            "status":          t["status"],
            "rejected_reason": t["rejected_reason"],
            "fill_mode":       t["fill_mode"],
        })

    return {
        "positions":  positions,
        "nav_series": nav_series,
        "trades":     trades,
    }


def pool_consults_payload(con: sqlite3.Connection, pool_id: str) -> dict:
    """
    GET /api/pools/{id}/consults
    回傳資金池的會診記錄清單（含各 agent 意見與主席摘要）。
    """
    consults_raw = con.execute(
        "SELECT id, symbol, as_of, question, summary, followed, outcome_7d, created_at "
        "FROM pool_consults WHERE pool_id=? ORDER BY id DESC",
        (pool_id,)
    ).fetchall()

    consults = []
    for c in consults_raw:
        opinions_raw = con.execute(
            "SELECT agent_id, stance, confidence, opinion "
            "FROM pool_consult_opinions WHERE consult_id=?",
            (c["id"],)
        ).fetchall()
        opinions = [dict(o) for o in opinions_raw]
        consults.append({
            "consult_id":  c["id"],
            "symbol":      c["symbol"],
            "as_of":       c["as_of"],
            "question":    c["question"],
            "summary":     c["summary"],
            "followed":    c["followed"],
            "outcome_7d":  c["outcome_7d"],
            "created_at":  c["created_at"],
            "opinions":    opinions,
        })

    return {"consults": consults}


def market_board_payload(con: sqlite3.Connection) -> dict:
    """
    GET /api/pools/market
    行情看盤板：涵蓋 prices 表中所有 symbol 的最新日線資料。
    回傳 as_of、每檔 close / prev_close / chg_pct / chg_5d_pct /
    volume / in_watchlist / mention_count / spark（最近 30 日收盤）。
    全部以少量 SQL 批次完成，不做每檔 N 次查詢。
    """
    # ── 1. as_of = prices 最大 date ──────────────────────────────────────────
    as_of_row = con.execute("SELECT MAX(date) FROM prices").fetchone()
    as_of: str = as_of_row[0] if as_of_row and as_of_row[0] else ""

    if not as_of:
        return {"as_of": "", "rows": []}

    # ── 2. 最新收盤（t0）與前一交易日收盤（t-1）─────────────────────────────
    # 每個 symbol 最新兩列 close；用 ROW_NUMBER 分組取前2
    t01_rows = con.execute(
        """
        WITH ranked AS (
          SELECT symbol, date, close,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
          FROM prices
        )
        SELECT symbol, date, close, rn
        FROM ranked
        WHERE rn <= 2
        ORDER BY symbol, rn
        """
    ).fetchall()

    sym_t0: dict = {}
    sym_t1: dict = {}
    for r in t01_rows:
        sym = r[0]
        rn = r[3]
        c = r[2]
        if rn == 1:
            sym_t0[sym] = c
        else:
            sym_t1[sym] = c

    # ── 3. 5 個交易日前收盤（t-5）─────────────────────────────────────────────
    t5_rows = con.execute(
        """
        WITH ranked AS (
          SELECT symbol, close,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
          FROM prices
        )
        SELECT symbol, close
        FROM ranked
        WHERE rn = 6
        """
    ).fetchall()
    sym_t5: dict = {r[0]: r[1] for r in t5_rows}

    # ── 4. 最新成交量 ──────────────────────────────────────────────────────────
    vol_rows = con.execute(
        """
        WITH ranked AS (
          SELECT symbol, volume,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
          FROM prices
        )
        SELECT symbol, volume
        FROM ranked
        WHERE rn = 1
        """
    ).fetchall()
    sym_vol: dict = {r[0]: r[1] for r in vol_rows}

    # ── 5. watchlist ──────────────────────────────────────────────────────────
    try:
        wl_rows = con.execute("SELECT symbol FROM watchlist").fetchall()
        wl_set = {r[0] for r in wl_rows}
    except Exception:
        wl_set = set()

    # ── 6. mention_count（全期計數）───────────────────────────────────────────
    mention_rows = con.execute(
        "SELECT symbol, COUNT(*) AS cnt FROM mentions GROUP BY symbol"
    ).fetchall()
    mention_map: dict = {r[0]: r[1] for r in mention_rows}

    # ── 7. spark：每 symbol 最近 30 個交易日 close（舊→新）─────────────────────
    spark_rows = con.execute(
        """
        WITH ranked AS (
          SELECT symbol, date, close,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
          FROM prices
        )
        SELECT symbol, date, close
        FROM ranked
        WHERE rn <= 30
        ORDER BY symbol, date ASC
        """
    ).fetchall()
    spark_map: dict = {}
    for r in spark_rows:
        sym = r[0]
        c = r[2]
        if sym not in spark_map:
            spark_map[sym] = []
        spark_map[sym].append(c)

    # ── 8. tw_symbols name lookup (c2-R3) ────────────────────────────────────
    try:
        tw_name_rows = con.execute(
            "SELECT yahoo_symbol, name FROM tw_symbols"
        ).fetchall()
        tw_name_map: dict = {r[0]: r[1] for r in tw_name_rows}
    except Exception:
        tw_name_map = {}

    # ── 9. 組裝 rows ─────────────────────────────────────────────────────────
    symbols = sorted(sym_t0.keys())
    rows = []
    for sym in symbols:
        close = sym_t0[sym]
        prev_close = sym_t1.get(sym)
        chg_pct = None
        if close is not None and prev_close is not None and prev_close != 0:
            chg_pct = round((close / prev_close - 1) * 100, 4)
        t5 = sym_t5.get(sym)
        chg_5d_pct = None
        if close is not None and t5 is not None and t5 != 0:
            chg_5d_pct = round((close / t5 - 1) * 100, 4)
        rows.append({
            "symbol":        sym,
            "name":          tw_name_map.get(sym),  # c2-R3: 台股中文簡稱，美股 None
            "close":         round(close, 4) if close is not None else None,
            "prev_close":    round(prev_close, 4) if prev_close is not None else None,
            "chg_pct":       chg_pct,
            "chg_5d_pct":    chg_5d_pct,
            "volume":        sym_vol.get(sym),
            "in_watchlist":  sym in wl_set,
            "mention_count": mention_map.get(sym, 0),
            "spark":         spark_map.get(sym, []),
            "region":        region(sym),
        })

    return {"as_of": as_of, "rows": rows}


def _calc_mdd(navs: list[float]) -> float:
    """MDD（最大回撤百分點，正值）。"""
    if len(navs) < 2:
        return 0.0
    peak = navs[0]
    mdd = 0.0
    for nav in navs:
        if nav > peak:
            peak = nav
        if peak > 0:
            dd = (peak - nav) / peak * 100.0
            if dd > mdd:
                mdd = dd
    return round(mdd, 4)
