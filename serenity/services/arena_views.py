"""
serenity/services/arena_views.py
arena_leaderboard_payload, arena_nav_payload, arena_trades_payload,
arena_reflections_payload
（原 server.py 2895-3071 行）
"""


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

    # sample_days: distinct dates in agent_nav_daily
    sample_days = 0
    try:
        row = con.execute("select count(distinct date) from agent_nav_daily").fetchone()
        sample_days = row[0] if row else 0
    except Exception:
        pass

    return {"month": month, "rows": computed, "sample_days": sample_days}


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
