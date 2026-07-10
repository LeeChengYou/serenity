"""
serenity/services/market.py
summary, symbol_payload, news_payload, fundamentals_payload,
estimates_payload, changes_payload
（原 server.py 1310-1368 + 1508-1604 + 2172-2256 + 2337-2370 行）
"""
from datetime import datetime

from ..db import _table_exists, one
from ..quant import _compute_indicators


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
