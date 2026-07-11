"""
serenity/services/pool_views.py
資金池 view 層：pool_list_payload / pool_detail_payload / pool_consults_payload
"""
from __future__ import annotations

import sqlite3


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
