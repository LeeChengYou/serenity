"""
serenity/services/hitrate.py
_hitrate_lock, _compute_live_hitrate, _compute_reconstructed_hitrate, hitrate_payload
（原 server.py 1724-2170 行）
"""
import json
import threading
from datetime import datetime
from pathlib import Path

from ..config import DB_PATH


# Lock to prevent concurrent hitrate reconstruction
_hitrate_lock = threading.Lock()


def _compute_live_hitrate(con) -> dict:
    """
    Compute hit rates from live signal_history rows (accumulated since 2026-07-04).

    A row is "matured" when a price 30 calendar days after its date is available.
    hit definition:
      BUY_TRIGGER / BUY_WATCH: hit = fwd_return > universe median on that date
      EXIT_ALERT:              hit = fwd_return < universe median on that date
      HOLD / NEUTRAL / OVERBOUGHT: excluded from hit counting (shown in summary only)

    Returns the "live" section of the hitrate response.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")

    try:
        rows = con.execute("""
            select symbol, date, signal, close as entry_close
            from signal_history
            where symbol not in ('SPY', 'SOXX', 'QQQ')
              and close is not null and close > 0
            order by date
        """).fetchall()
    except Exception as exc:
        print(f"[hitrate live] query failed: {exc}")
        return {"label": "live (signal_history)", "rows": [], "n_total": 0, "as_of": as_of}

    if not rows:
        return {"label": "live (signal_history)", "rows": [], "n_total": 0, "as_of": as_of}

    # Build per-date universe medians (lazy computed)
    date_univ_med: dict = {}

    def _get_univ_median(date_str: str):
        if date_str in date_univ_med:
            return date_univ_med[date_str]
        try:
            # For each signal_history row on this date, compute 30d forward return
            sh_rows = con.execute("""
                select symbol, close
                from signal_history
                where date=? and close is not null and close > 0
                  and symbol not in ('SPY', 'SOXX', 'QQQ')
            """, (date_str,)).fetchall()
            rets = []
            for sr in sh_rows:
                sym, entry = sr[0], sr[1]
                exit_row = con.execute("""
                    select close from prices
                    where symbol=? and date > ? and date <= date(?, '+30 days')
                    order by date desc limit 1
                """, (sym, date_str, date_str)).fetchone()
                if exit_row and exit_row[0] and entry > 0:
                    rets.append(exit_row[0] / entry - 1.0)
            med = None
            if rets:
                srt = sorted(rets)
                mid = len(srt) // 2
                med = srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2
            date_univ_med[date_str] = med
        except Exception:
            date_univ_med[date_str] = None
        return date_univ_med[date_str]

    # Per-signal buckets + per-call records (for the recent_calls contract field)
    _EXCLUDED_SIGNALS = {"HOLD", "NEUTRAL", "OVERBOUGHT"}
    from collections import defaultdict
    buckets: dict = defaultdict(lambda: {"n": 0, "matured": 0, "hits": 0, "excess": [], "fwds": []})
    calls: list = []

    for row in rows:
        sym, date_str, sig, entry_close = row[0], row[1], row[2], row[3]

        if sig in _EXCLUDED_SIGNALS:
            continue

        buckets[sig]["n"] += 1

        # Find exit close
        try:
            exit_row = con.execute("""
                select close from prices
                where symbol=? and date > ? and date <= date(?, '+30 days')
                order by date desc limit 1
            """, (sym, date_str, date_str)).fetchone()
        except Exception:
            exit_row = None

        exit_close = exit_row[0] if exit_row else None
        univ_med = _get_univ_median(date_str) if exit_close is not None else None

        fwd_return = None
        hit = None
        if exit_close is not None and univ_med is not None:
            fwd_return = exit_close / entry_close - 1.0
            buckets[sig]["matured"] += 1
            buckets[sig]["excess"].append(fwd_return - univ_med)
            buckets[sig]["fwds"].append(fwd_return)
            if sig in ("BUY_TRIGGER", "BUY_WATCH"):
                hit = fwd_return > univ_med
            elif sig == "EXIT_ALERT":
                hit = fwd_return < univ_med
            if hit:
                buckets[sig]["hits"] += 1

        calls.append({
            "symbol": sym,
            "date": date_str,
            "signal": sig,
            "close_then": entry_close,
            "close_now": exit_close,
            "fwd_return": round(fwd_return, 4) if fwd_return is not None else None,
            "universe_return": round(univ_med, 4) if univ_med is not None else None,
            "hit": hit,
            "source": "live",
        })

    def _median(vals):
        if not vals:
            return None
        s = sorted(vals)
        m = len(s) // 2
        return s[m] if len(s) % 2 == 1 else (s[m - 1] + s[m]) / 2

    summary_rows = []
    for sig, b in buckets.items():
        n_matured = b["matured"]
        hits = b["hits"]
        insufficient = n_matured < 10
        win_rate = None if (insufficient or n_matured == 0) else round(hits / n_matured, 4)
        med_exc = _median(b["excess"])
        med_fwd = _median(b["fwds"])
        summary_rows.append({
            "signal":      sig,
            "n":           b["n"],
            "n_matured":   n_matured,
            "hits":        hits if not insufficient else None,
            "win_rate":    win_rate,
            "median_fwd_return_30d": round(med_fwd, 4) if (med_fwd is not None and not insufficient) else None,
            "vs_universe": round(med_exc, 4) if (med_exc is not None and not insufficient) else None,
            "insufficient": insufficient,
        })

    # Sort by signal name for deterministic output
    summary_rows.sort(key=lambda x: x["signal"])
    calls.sort(key=lambda c: c["date"], reverse=True)

    return {
        "label":   "live (signal_history, 開始日期 2026-07-04)",
        "rows":    summary_rows,
        "calls":   calls,
        "n_total": len(rows),
        "as_of":   as_of,
    }


def _compute_reconstructed_hitrate(con, db_path: Path) -> dict:
    """
    Reconstruct hit rates using the multiwindow point-in-time machinery.

    Reuses backtest_multiwindow.py's evaluate_symbol_at_cutoff discipline:
    - Bars truncated to <= cutoff before indicator computation (zero look-ahead)
    - score_symbol called with now=cutoff
    - Exit = last close within (cutoff, cutoff + 30 days]

    Results are cached in hitrate_cache; invalidated when max price date advances.

    hit definition (per spec):
      BUY_TRIGGER / BUY_WATCH: fwd_return > universe median same window
      EXIT_ALERT:              fwd_return < universe median same window
      HOLD / NEUTRAL / OVERBOUGHT: excluded from hit counting
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    _EXCLUDED = {"HOLD", "NEUTRAL", "OVERBOUGHT"}
    empty = {
        "label": "reconstructed (point-in-time multiwindow)",
        "cutoff_count": 0,
        "total_obs": 0,
        "rows": [],
        "as_of": as_of,
    }

    # Current max price date (non-benchmark)
    try:
        row = con.execute(
            "select max(date) from prices where symbol not in ('SPY','SOXX','QQQ')"
        ).fetchone()
        max_price_date = row[0] if row and row[0] else None
    except Exception:
        max_price_date = None

    if not max_price_date:
        return {**empty, "note": "No price data available for reconstruction."}

    # Check cache (v2: rows carry vs_universe median excess)
    try:
        cached = con.execute(
            "select cache_json, max_price_date from hitrate_cache where cache_key='reconstructed_v2'"
        ).fetchone()
        if cached and cached[1] == max_price_date:
            try:
                return json.loads(cached[0])
            except Exception:
                pass
    except Exception:
        pass

    # Load multiwindow module (lazy, so server startup stays fast)
    try:
        import importlib.util as _mw_ilu
        from ..config import ROOT
        _mw_spec = _mw_ilu.spec_from_file_location(
            "backtest_multiwindow",
            ROOT / "scripts" / "backtest_multiwindow.py",
        )
        _mw_mod = _mw_ilu.module_from_spec(_mw_spec)
        _mw_spec.loader.exec_module(_mw_mod)
    except Exception as exc:
        print(f"[hitrate recon] Failed to load backtest_multiwindow: {exc}")
        return {**empty, "note": f"Reconstruction unavailable: {exc}"}

    try:
        import sqlite3 as _sq3
        _recon_con = _sq3.connect(str(db_path))
        _recon_con.row_factory = _sq3.Row

        # Load all prices, excluding benchmarks
        all_rows = _recon_con.execute(
            "SELECT symbol, date, open, high, low, close, volume "
            "FROM prices "
            "WHERE close IS NOT NULL AND close > 0 "
            "  AND symbol NOT IN ('SPY', 'SOXX', 'QQQ') "
            "ORDER BY symbol, date"
        ).fetchall()
        _recon_con.close()
    except Exception as exc:
        print(f"[hitrate recon] Failed to load prices: {exc}")
        return {**empty, "note": f"Price load failed: {exc}"}

    # Build all_prices dict
    all_prices: dict = {}
    for r in all_rows:
        sym = r[0]
        if sym not in all_prices:
            all_prices[sym] = []
        all_prices[sym].append({
            "symbol": sym, "date": r[1],
            "open": r[2], "high": r[3], "low": r[4],
            "close": r[5], "volume": r[6],
        })

    if not all_prices:
        return {**empty, "note": "No price data for reconstruction."}

    # Enumerate cutoffs (every 7 days, matching spec)
    try:
        cutoffs = _mw_mod._enumerate_cutoffs(
            all_prices, max_price_date,
            step_days=7, min_symbols=10, min_bars=60, horizon_days=30,
        )
    except Exception as exc:
        print(f"[hitrate recon] cutoff enumeration failed: {exc}")
        cutoffs = []

    if not cutoffs:
        result = {**empty, "note": "Insufficient history to enumerate cutoffs (need ≥10 symbols with ≥60 bars)."}
        return result

    print(f"[hitrate recon] Running {len(cutoffs)} cutoffs (step=7d, horizon=30d)…")

    # Per-signal accumulators
    from collections import defaultdict
    buckets: dict = defaultdict(lambda: {"n_total": 0, "returns": [], "excess": [], "hits": 0})
    total_obs = 0

    for cutoff_str in cutoffs:
        try:
            win = _mw_mod.run_window_fixed_horizon(
                db_path, all_prices, cutoff_str, horizon_days=30, min_bars=60
            )
        except Exception as exc:
            print(f"[hitrate recon] window {cutoff_str} failed: {exc}")
            continue

        records = win.get("records", [])
        # Universe returns for this window
        win_rets = [r["holdout_return"] for r in records if r.get("holdout_return") is not None]
        if not win_rets:
            continue
        srt = sorted(win_rets)
        mid = len(srt) // 2
        univ_med = srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2

        for rec in records:
            sig = rec.get("signal")
            hret = rec.get("holdout_return")
            if sig is None:
                continue
            if sig in _EXCLUDED:
                continue
            total_obs += 1
            buckets[sig]["n_total"] += 1
            if hret is not None:
                buckets[sig]["returns"].append(hret)
                buckets[sig]["excess"].append(hret - univ_med)
                if sig in ("BUY_TRIGGER", "BUY_WATCH") and hret > univ_med:
                    buckets[sig]["hits"] += 1
                elif sig == "EXIT_ALERT" and hret < univ_med:
                    buckets[sig]["hits"] += 1

    def _median(vals):
        if not vals:
            return None
        s = sorted(vals)
        m = len(s) // 2
        return s[m] if len(s) % 2 == 1 else (s[m - 1] + s[m]) / 2

    summary_rows = []
    for sig, b in buckets.items():
        n = len(b["returns"])
        hits = b["hits"]
        insufficient = n < 10
        win_rate = None if (insufficient or n == 0) else round(hits / n, 4)
        med_exc = _median(b["excess"])
        med_fwd = _median(b["returns"])
        summary_rows.append({
            "signal":      sig,
            "n":           b["n_total"],
            "n_with_exit": n,
            "hits":        hits if not insufficient else None,
            "win_rate":    win_rate,
            "median_fwd_return_30d": round(med_fwd, 4) if (med_fwd is not None and not insufficient) else None,
            "vs_universe": round(med_exc, 4) if (med_exc is not None and not insufficient) else None,
            "insufficient": insufficient,
        })

    summary_rows.sort(key=lambda x: x["signal"])

    result = {
        "label":        "reconstructed (point-in-time multiwindow)",
        "cutoff_count": len(cutoffs),
        "total_obs":    total_obs,
        "rows":         summary_rows,
        "as_of":        as_of,
    }

    # Cache result
    try:
        con.execute(
            """insert into hitrate_cache(cache_key, max_price_date, cache_json, computed_at)
               values ('reconstructed_v2', ?, ?, ?)
               on conflict(cache_key) do update set
                   max_price_date=excluded.max_price_date,
                   cache_json=excluded.cache_json,
                   computed_at=excluded.computed_at""",
            (max_price_date, json.dumps(result, ensure_ascii=False), datetime.now().isoformat()),
        )
        con.commit()
        print(f"[hitrate recon] Cached reconstruction result ({len(cutoffs)} cutoffs, {total_obs} obs)")
    except Exception as exc:
        print(f"[hitrate recon] Cache write failed: {exc}")

    return result


def hitrate_payload(con) -> dict:
    """
    GET /api/hitrate

    Returns hit-rate analysis from two honestly labeled sources:
      "live":         signal_history rows (started 2026-07-04) with 30d forward
      "reconstructed": point-in-time multiwindow reconstruction (cached)

    Never 500s — degrades gracefully to empty rows.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    try:
        live = _compute_live_hitrate(con)
    except Exception as exc:
        print(f"[hitrate_payload] live source failed: {exc}")
        live = {"label": "live (signal_history)", "rows": [], "n_total": 0, "as_of": as_of, "error": str(exc)}

    with _hitrate_lock:
        try:
            recon = _compute_reconstructed_hitrate(con, DB_PATH)
        except Exception as exc:
            print(f"[hitrate_payload] reconstructed source failed: {exc}")
            recon = {
                "label": "reconstructed (point-in-time multiwindow)",
                "cutoff_count": 0,
                "total_obs": 0,
                "rows": [],
                "as_of": as_of,
                "error": str(exc),
            }

    # Assemble the spec contract (REQUIREMENTS_V3.md R3-1):
    # {"as_of","live_since","summary":[...rows with source...],"recent_calls":[...]}
    live_since = None
    try:
        row = con.execute("select min(date) from signal_history").fetchone()
        live_since = row[0] if row else None
    except Exception:
        pass

    summary = []
    for r in live.get("rows", []):
        summary.append({
            "signal": r.get("signal"),
            "n": r.get("n_matured", r.get("n")),
            "n_pending": (r.get("n") or 0) - (r.get("n_matured") or 0),
            "median_fwd_return_30d": r.get("median_fwd_return_30d"),
            "win_rate": r.get("win_rate"),
            "vs_universe": r.get("vs_universe"),
            "insufficient": r.get("insufficient", False),
            "source": "live",
        })
    for r in recon.get("rows", []):
        summary.append({
            "signal": r.get("signal"),
            "n": r.get("n_with_exit", r.get("n")),
            "n_pending": 0,
            "median_fwd_return_30d": r.get("median_fwd_return_30d"),
            "win_rate": r.get("win_rate"),
            "vs_universe": r.get("vs_universe"),
            "insufficient": r.get("insufficient", False),
            "source": "reconstructed",
        })

    # sample_days: distinct dates in signal_history
    sample_days = 0
    try:
        row = con.execute("select count(distinct date) from signal_history").fetchone()
        sample_days = row[0] if row else 0
    except Exception:
        pass

    return {
        "as_of": as_of,
        "live_since": live_since,
        "sample_days": sample_days,
        "summary": summary,
        "recent_calls": live.get("calls", [])[:50],
        # Kept for transparency/debugging: full per-source detail
        "sources": {
            "live":          {k: v for k, v in live.items() if k != "calls"},
            "reconstructed": recon,
        },
    }
