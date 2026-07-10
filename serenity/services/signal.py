"""
serenity/services/signal.py
signal_payload, snapshot_signals
（原 server.py 1371-1496 + 2791-2892 行）
"""
from datetime import datetime

from ..config import DB_PATH
from ..db import db
from ..quant import BENCHMARK_SYMBOLS, _compute_indicators, _quant_score, _evaluate_signal


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
