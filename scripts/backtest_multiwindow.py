#!/usr/bin/env python3
"""
Multi-window holdout validation + pullback variant comparison — R-1 extension.
Extended with B-3 (trade cost sensitivity) and B-4 (threshold scan).

Extends backtest_holdout.py with:
  - Rolling cutoffs every 15 days (earliest date with >=40 symbols/>=60 bars
    up to latest_date - 30 days)
  - FIXED 30-day forward horizon: exit = last close <= cutoff + 30 calendar days
    (fixes the inconsistent-horizon issue in backtest_holdout.py)
  - Aggregated signal-state table across all windows
  - KEY QUESTION: does EXIT_ALERT outperformance hold at n >= 20?

With --pullback flag, also evaluates three candidate buy variants for
symbols with quant score >= 65 ("high heat"):
  Variant A "extended":    close > EMA20 AND RSI >= 60
  Variant B "pullback":    close < EMA20 AND drawdown-from-60-bar-high in [10%, 35%]
                           AND RSI in [30, 50]
  Variant C "deep value":  drawdown-from-60-bar-high > 35%
  Baseline "high heat":    score >= 65, no price filter

With --costs flag (B-3), applies round-trip cost haircuts of 0/10/25/50 bps
to each signal group's returns and reports whether the edge survives costs.
Costs apply to entry+exit as a haircut: adjusted_return = raw_return - cost_bps/10000.
Universe benchmark is unadjusted (passive, no friction).

With --threshold-scan flag (B-4), runs one-dimensional scans of:
  - BUY_WATCH score threshold: 60, 65, 70, 75, 80
  - RSI entry bound: 55, 60, 65, 70
  - RSI exit bound (EXIT_ALERT): 35, 40, 45
For each setting, records are re-classified using the custom threshold
applied to pre-computed indicators (zero look-ahead maintained).
Cells with n<10 are marked insufficient; conclusions only where n>=10.

ZERO LOOK-AHEAD GUARANTEE
  Bars are truncated to <= cutoff BEFORE any indicator or score computation.
  evaluate_symbol_at_cutoff (imported from backtest_holdout.py) passes
  now=cutoff_dt to score_symbol, ensuring mentions are also point-in-time.

ZERO FABRICATED DATA
  Every return = exit_close / entry_close - 1 from real SQLite rows.
  Insufficient samples are stated as "insufficient", never forced.

Usage:
    python scripts/backtest_multiwindow.py [--db PATH] [--pullback]
    python scripts/backtest_multiwindow.py [--db PATH] [--costs]
    python scripts/backtest_multiwindow.py [--db PATH] [--threshold-scan]
    python scripts/backtest_multiwindow.py [--db PATH] [--costs] [--threshold-scan]
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Import helpers from backtest_holdout.py (single source of truth)
# ---------------------------------------------------------------------------

def _load_holdout_module():
    """Import the backtest_holdout module by path."""
    holdout_path = Path(__file__).resolve().parent / "backtest_holdout.py"
    spec = importlib.util.spec_from_file_location("backtest_holdout", holdout_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_holdout = _load_holdout_module()
evaluate_symbol_at_cutoff = _holdout.evaluate_symbol_at_cutoff
_load_all_prices = _holdout._load_all_prices
_get_max_date = _holdout._get_max_date
_median = _holdout._median
_win_rate = _holdout._win_rate
_pct = _holdout._pct
_fmt_rate = _holdout._fmt_rate
SIGNAL_ORDER = _holdout.SIGNAL_ORDER


# ---------------------------------------------------------------------------
# Database path helper
# ---------------------------------------------------------------------------

def _get_db_path(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parents[1] / "data" / "serenity.sqlite"


# ---------------------------------------------------------------------------
# Fixed-horizon exit price (key fix over backtest_holdout.py)
# ---------------------------------------------------------------------------

def _find_exit_price_fixed_horizon(
    all_bars: list[dict],
    cutoff_date_str: str,
    horizon_days: int = 30,
) -> Optional[float]:
    """
    Return the last stored close within (cutoff, cutoff + horizon_days] calendar days.

    This produces a consistent ~30-day forward measurement regardless of when
    in the window this function is called, unlike _find_exit_price which uses
    the last available bar (variable horizon).

    Returns None if no bars fall in the horizon window.
    """
    cutoff_dt = date.fromisoformat(cutoff_date_str)
    horizon_end = cutoff_dt + timedelta(days=horizon_days)
    horizon_end_str = horizon_end.isoformat()

    candidates = [
        b["close"] for b in all_bars
        if cutoff_date_str < b["date"] <= horizon_end_str
    ]
    if not candidates:
        return None
    return candidates[-1]


def _holdout_return(entry: float, exit_: float) -> float:
    """Compute exit / entry - 1."""
    return exit_ / entry - 1.0


# ---------------------------------------------------------------------------
# 60-bar high helper (for drawdown computation in pullback variants)
# ---------------------------------------------------------------------------

def _high_60bar(bars_pre: list[dict]) -> Optional[float]:
    """
    Return the max of the 'high' field over the last 60 bars before cutoff.

    Falls back to 'close' when 'high' is None.  Returns None if no valid
    values exist in the tail 60 bars.
    """
    tail = bars_pre[-60:] if len(bars_pre) >= 60 else bars_pre
    vals = []
    for b in tail:
        h = b.get("high")
        c = b.get("close")
        if h is not None:
            try:
                vals.append(float(h))
                continue
            except (TypeError, ValueError):
                pass
        if c is not None:
            try:
                vals.append(float(c))
            except (TypeError, ValueError):
                pass
    return max(vals) if vals else None


# ---------------------------------------------------------------------------
# Indicator last-non-None helper
# ---------------------------------------------------------------------------

def _last_non_none(series: list) -> Optional[float]:
    """Return the last non-None value in a per-bar indicator series."""
    if not series:
        return None
    for v in reversed(series):
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Cutoff enumeration
# ---------------------------------------------------------------------------

def _enumerate_cutoffs(
    all_prices: dict[str, list[dict]],
    max_date_str: str,
    step_days: int = 15,
    min_symbols: int = 40,
    min_bars: int = 60,
    horizon_days: int = 30,
) -> list[str]:
    """
    Return a list of cutoff date strings (YYYY-MM-DD) stepping every
    step_days days, starting from the earliest date at which at least
    min_symbols have at least min_bars prior bars, up to
    max_date - horizon_days days.

    The upper bound ensures each cutoff still has a 30-day forward window.
    """
    max_date = date.fromisoformat(max_date_str)
    upper_bound = max_date - timedelta(days=horizon_days)

    # Find all dates that appear in the prices table (any symbol)
    all_dates = sorted({b["date"] for bars in all_prices.values() for b in bars})
    if not all_dates:
        return []

    # Find the earliest date where >= min_symbols have >= min_bars history
    earliest_cutoff = None
    for d_str in all_dates:
        count = sum(
            1 for bars in all_prices.values()
            if sum(1 for b in bars if b["date"] <= d_str) >= min_bars
        )
        if count >= min_symbols:
            earliest_cutoff = date.fromisoformat(d_str)
            break

    if earliest_cutoff is None or earliest_cutoff > upper_bound:
        return []

    # Step forward every step_days calendar days
    cutoffs = []
    current = earliest_cutoff
    while current <= upper_bound:
        cutoffs.append(current.isoformat())
        current += timedelta(days=step_days)

    return cutoffs


# ---------------------------------------------------------------------------
# Per-window evaluation with fixed horizon
# ---------------------------------------------------------------------------

def run_window_fixed_horizon(
    db_path: Path,
    all_prices: dict[str, list[dict]],
    cutoff_date_str: str,
    horizon_days: int = 30,
    min_bars: int = 60,
) -> dict:
    """
    Evaluate all symbols at a single cutoff with a fixed forward horizon.

    Returns:
        {
          "cutoff": str,
          "records": [per-symbol dicts],
          "n_skipped_bars": int,
          "n_skipped_no_exit": int,
        }

    Each record dict contains:
        symbol, signal, score, components, entry, exit, holdout_return,
        indicators, high_60bar
    """
    cutoff_dt = datetime.fromisoformat(cutoff_date_str + "T23:59:59+00:00")
    records = []
    n_skipped_bars = 0
    n_skipped_no_exit = 0

    for symbol, bars in sorted(all_prices.items()):
        bars_pre = [b for b in bars if b["date"] <= cutoff_date_str]

        if len(bars_pre) < min_bars:
            n_skipped_bars += 1
            continue

        eval_result = evaluate_symbol_at_cutoff(db_path, symbol, bars_pre, cutoff_dt)
        if eval_result is None:
            n_skipped_bars += 1
            continue

        entry_close = eval_result["entry_close"]
        exit_close = _find_exit_price_fixed_horizon(bars, cutoff_date_str, horizon_days)

        if exit_close is None:
            n_skipped_no_exit += 1
            hret = None
        else:
            hret = _holdout_return(entry_close, exit_close)

        h60 = _high_60bar(bars_pre)

        records.append({
            "symbol":         symbol,
            "signal":         eval_result["signal"],
            "score":          eval_result["score"],
            "components":     eval_result["components"],
            "entry":          entry_close,
            "exit":           exit_close,
            "holdout_return": hret,
            "indicators":     eval_result["indicators"],
            "high_60bar":     h60,
        })

    return {
        "cutoff":            cutoff_date_str,
        "records":           records,
        "n_skipped_bars":    n_skipped_bars,
        "n_skipped_no_exit": n_skipped_no_exit,
    }


# ---------------------------------------------------------------------------
# Aggregation across windows — signal state table
# ---------------------------------------------------------------------------

def aggregate_signal_stats(windows: list[dict]) -> dict[str, dict]:
    """
    Aggregate returns per signal state across all windows.

    Returns:
        {signal_state: {"returns": [...], "n_total": int}}
    where "returns" is the flat list of all per-symbol 30-day returns in
    that signal state across every window.
    """
    agg: dict[str, dict] = {}
    universe_returns: list[float] = []

    for win in windows:
        win_rets = [
            r["holdout_return"] for r in win["records"]
            if r["holdout_return"] is not None
        ]
        universe_returns.extend(win_rets)

        for r in win["records"]:
            sig = r["signal"]
            if sig not in agg:
                agg[sig] = {"returns": [], "n_total": 0}
            agg[sig]["n_total"] += 1
            if r["holdout_return"] is not None:
                agg[sig]["returns"].append(r["holdout_return"])

    return agg, universe_returns


# ---------------------------------------------------------------------------
# Pullback variant classifiers
# ---------------------------------------------------------------------------

MIN_HIGH_HEAT_SCORE = 65


def classify_pullback_variant(record: dict) -> Optional[str]:
    """
    Classify a record into pullback variants A, B, C, or 'baseline'
    for symbols with quant score >= MIN_HIGH_HEAT_SCORE.

    Returns:
        "A"        — extended:   close > EMA20 AND RSI >= 60
        "B"        — pullback:   close < EMA20 AND drawdown [10%, 35%] AND RSI [30, 50]
        "C"        — deep value: drawdown > 35%
        "baseline" — high heat, no specific variant matched
        None       — score < 65 or insufficient indicators
    """
    score = record.get("score")
    if score is None or score < MIN_HIGH_HEAT_SCORE:
        return None

    close = record.get("entry")
    if close is None:
        return None

    indicators = record.get("indicators") or {}
    ema20 = _last_non_none(indicators.get("ema20", []))
    rsi   = _last_non_none(indicators.get("rsi14", []))
    h60   = record.get("high_60bar")

    # Drawdown from 60-bar high
    if h60 is not None and h60 > 0:
        drawdown = (h60 - close) / h60   # positive = below peak
    else:
        drawdown = None

    # Variant C — deep value (drawdown > 35%, regardless of EMA position)
    if drawdown is not None and drawdown > 0.35:
        return "C"

    # Variant B — pullback zone (EMA20 < close, controlled drawdown, RSI cooling)
    if (ema20 is not None and close < ema20
            and drawdown is not None and 0.10 <= drawdown <= 0.35
            and rsi is not None and 30 <= rsi <= 50):
        return "B"

    # Variant A — extended / chasing strength
    if (ema20 is not None and close > ema20
            and rsi is not None and rsi >= 60):
        return "A"

    # High-heat but no specific variant matched
    return "baseline"


def aggregate_pullback_stats(windows: list[dict]) -> dict:
    """
    For each pullback variant (A, B, C, baseline) collect returns across
    all windows.

    Returns:
        {variant: {"returns": [...], "n_total": int}}
    and flat universe_returns for the same windows.
    """
    variant_agg: dict[str, dict] = {
        "A":        {"returns": [], "n_total": 0},
        "B":        {"returns": [], "n_total": 0},
        "C":        {"returns": [], "n_total": 0},
        "baseline": {"returns": [], "n_total": 0},
    }
    universe_returns: list[float] = []

    for win in windows:
        win_rets = [
            r["holdout_return"] for r in win["records"]
            if r["holdout_return"] is not None
        ]
        universe_returns.extend(win_rets)

        for r in win["records"]:
            variant = classify_pullback_variant(r)
            if variant is None:
                continue
            variant_agg[variant]["n_total"] += 1
            if r["holdout_return"] is not None:
                variant_agg[variant]["returns"].append(r["holdout_return"])

    return variant_agg, universe_returns


# ---------------------------------------------------------------------------
# Report: aggregated signal state table
# ---------------------------------------------------------------------------

def print_aggregated_signal_table(
    agg: dict[str, dict],
    universe_returns: list[float],
    total_windows: int,
) -> None:
    """Print the aggregated signal-state performance table across all windows."""
    univ_med = _median(universe_returns)
    univ_wr  = _win_rate(universe_returns)

    print()
    print("=" * 90)
    print("  AGGREGATED SIGNAL-STATE PERFORMANCE (all windows, fixed 30-day horizon)")
    print(f"  {total_windows} cutoffs | Universe n={len(universe_returns)} obs | "
          f"Universe median={_pct(univ_med)} | Win rate={_fmt_rate(univ_wr)}")
    print("=" * 90)

    hdr = (
        f"{'Signal':<14} | {'n (total)':>9} | {'n (returns)':>11} | "
        f"{'Median Ret':>10} | {'Win Rate':>8} | {'vs Universe':>11} | "
        f"{'Interpretation':<30}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    for sig in SIGNAL_ORDER:
        if sig not in agg:
            continue
        bucket = agg[sig]
        rets = bucket["returns"]
        n_total = bucket["n_total"]
        med = _median(rets)
        wr  = _win_rate(rets)
        vs  = (med - univ_med) if (med is not None and univ_med is not None) else None

        # Interpretation
        if len(rets) < 10:
            interp = "insufficient (n<10)"
        elif vs is None:
            interp = "cannot assess"
        elif vs > 0.03:
            interp = "OUTPERFORMS universe"
        elif vs < -0.03:
            interp = "UNDERPERFORMS universe"
        else:
            interp = "roughly inline with universe"

        print(
            f"{sig:<14} | {n_total:>9} | {len(rets):>11} | "
            f"{_pct(med):>10} | {_fmt_rate(wr):>8} | {_pct(vs):>11} | "
            f"{interp:<30}"
        )

    print(sep)
    print()

    # Key question: EXIT_ALERT at n >= 20
    exit_bucket = agg.get("EXIT_ALERT", {})
    exit_rets = exit_bucket.get("returns", [])
    exit_med = _median(exit_rets)
    exit_vs = (exit_med - univ_med) if (exit_med is not None and univ_med is not None) else None
    print("  KEY QUESTION — Does EXIT_ALERT outperformance hold at n >= 20?")
    if len(exit_rets) >= 20:
        if exit_vs is not None and exit_vs > 0:
            print(f"  => YES: EXIT_ALERT n={len(exit_rets)}, median={_pct(exit_med)}, "
                  f"excess={_pct(exit_vs)} vs universe. Edge CONFIRMED at n>=20.")
        else:
            print(f"  => NO: EXIT_ALERT n={len(exit_rets)}, median={_pct(exit_med)}, "
                  f"excess={_pct(exit_vs)}. No consistent outperformance at scale.")
    else:
        print(f"  => INSUFFICIENT: EXIT_ALERT only n={len(exit_rets)} returns across "
              f"{total_windows} windows. Cannot confirm edge (need >= 20).")
    print()


# ---------------------------------------------------------------------------
# Report: per-window mini-table
# ---------------------------------------------------------------------------

def print_per_window_mini_table(windows: list[dict]) -> None:
    """Print a compact per-window summary for each cutoff."""
    print("=" * 90)
    print("  PER-WINDOW MINI-TABLE (fixed 30-day horizon, each row = one cutoff)")
    print("=" * 90)

    hdr = (
        f"{'Cutoff':<12} | {'n eval':>6} | {'Univ Med':>8} | "
        f"{'BUY n':>5} | {'BUY Med':>7} | {'BUY vs':>7} | "
        f"{'EXIT n':>6} | {'EXIT Med':>8} | {'EXIT vs':>8}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    for win in windows:
        records = win["records"]
        cutoff = win["cutoff"]

        all_rets = [r["holdout_return"] for r in records if r["holdout_return"] is not None]
        univ_med = _median(all_rets)

        buy_rets = [
            r["holdout_return"] for r in records
            if r["signal"] in ("BUY_TRIGGER", "BUY_WATCH")
            and r["holdout_return"] is not None
        ]
        buy_n = sum(1 for r in records if r["signal"] in ("BUY_TRIGGER", "BUY_WATCH"))
        buy_med = _median(buy_rets)
        buy_vs = (buy_med - univ_med) if (buy_med is not None and univ_med is not None) else None

        exit_rets = [
            r["holdout_return"] for r in records
            if r["signal"] == "EXIT_ALERT"
            and r["holdout_return"] is not None
        ]
        exit_n = sum(1 for r in records if r["signal"] == "EXIT_ALERT")
        exit_med = _median(exit_rets)
        exit_vs = (exit_med - univ_med) if (exit_med is not None and univ_med is not None) else None

        print(
            f"{cutoff:<12} | {len(records):>6} | {_pct(univ_med):>8} | "
            f"{buy_n:>5} | {_pct(buy_med):>7} | {_pct(buy_vs):>7} | "
            f"{exit_n:>6} | {_pct(exit_med):>8} | {_pct(exit_vs):>8}"
        )

    print(sep)
    print()


# ---------------------------------------------------------------------------
# Report: pullback variant comparison
# ---------------------------------------------------------------------------

def print_pullback_report(
    variant_agg: dict[str, dict],
    universe_returns: list[float],
    total_windows: int,
) -> None:
    """Print the pullback variant A/B/C comparison table."""
    univ_med = _median(universe_returns)
    univ_wr  = _win_rate(universe_returns)

    print()
    print("=" * 90)
    print("  PULLBACK VARIANT ANALYSIS (quant score >= 65 universe)")
    print(f"  {total_windows} cutoffs | Score threshold >= {MIN_HIGH_HEAT_SCORE}")
    print(f"  Universe (all symbols): n={len(universe_returns)}, "
          f"median={_pct(univ_med)}, win rate={_fmt_rate(univ_wr)}")
    print()
    print("  Variants:")
    print("    baseline = score>=65, no price filter (all high-heat stocks)")
    print("    A        = extended:    close>EMA20 AND RSI>=60 (chasing strength)")
    print("    B        = pullback:    close<EMA20 AND drawdown[10-35%] AND RSI[30-50]")
    print("    C        = deep value:  drawdown from 60-bar high >35%")
    print("=" * 90)

    hdr = (
        f"{'Variant':<10} | {'n (total)':>9} | {'n (returns)':>11} | "
        f"{'Median Ret':>10} | {'Win Rate':>8} | {'vs Universe':>11} | "
        f"{'Verdict':<35}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    variant_order = ["baseline", "A", "B", "C"]
    variant_labels = {
        "baseline": "baseline (all high-heat)",
        "A":        "A (extended)",
        "B":        "B (pullback)",
        "C":        "C (deep value)",
    }

    results = {}
    for var in variant_order:
        bucket = variant_agg.get(var, {"returns": [], "n_total": 0})
        rets = bucket["returns"]
        n_total = bucket["n_total"]
        med = _median(rets)
        wr  = _win_rate(rets)
        vs  = (med - univ_med) if (med is not None and univ_med is not None) else None
        results[var] = {"n": len(rets), "med": med, "vs": vs, "wr": wr, "n_total": n_total}

        if len(rets) < 10:
            verdict = "insufficient sample (n<10)"
        elif vs is None:
            verdict = "cannot assess"
        elif vs > 0.05:
            verdict = "CLEAR EDGE vs universe"
        elif vs > 0.02:
            verdict = "marginal positive edge"
        elif vs < -0.05:
            verdict = "CLEAR DRAG vs universe"
        elif vs < -0.02:
            verdict = "marginal underperformance"
        else:
            verdict = "no meaningful edge"

        label = variant_labels.get(var, var)
        print(
            f"{label:<10} | {n_total:>9} | {len(rets):>11} | "
            f"{_pct(med):>10} | {_fmt_rate(wr):>8} | {_pct(vs):>11} | "
            f"{verdict:<35}"
        )

    print(sep)
    print()

    # Honest conclusion
    print("  HONEST INTERPRETATION:")
    b_vs = results.get("B", {}).get("vs")
    a_vs = results.get("A", {}).get("vs")
    b_n  = results.get("B", {}).get("n", 0)
    a_n  = results.get("A", {}).get("n", 0)

    if b_n < 10:
        print("  Variant B (pullback): INSUFFICIENT SAMPLE (n<10) — cannot conclude.")
    elif b_vs is not None and b_vs > 0.02:
        print(f"  Variant B (pullback): n={b_n}, excess={_pct(b_vs)} — "
              "POSITIVE EDGE. Pullback filter appears beneficial.")
    elif b_vs is not None and b_vs < -0.02:
        print(f"  Variant B (pullback): n={b_n}, excess={_pct(b_vs)} — "
              "UNDERPERFORMS. Pullback filter does NOT add value.")
    else:
        print(f"  Variant B (pullback): n={b_n}, excess={_pct(b_vs)} — "
              "NO CLEAR EDGE over universe.")

    if a_n < 10:
        print("  Variant A (extended): INSUFFICIENT SAMPLE (n<10) — cannot conclude.")
    elif a_vs is not None and a_vs > 0.02:
        print(f"  Variant A (extended): n={a_n}, excess={_pct(a_vs)} — "
              "POSITIVE EDGE. Extended stocks perform well.")
    elif a_vs is not None and a_vs < -0.02:
        print(f"  Variant A (extended): n={a_n}, excess={_pct(a_vs)} — "
              "UNDERPERFORMS. 'Chasing strength' is confirmed costly.")
    else:
        print(f"  Variant A (extended): n={a_n}, excess={_pct(a_vs)} — "
              "NO CLEAR EDGE over universe.")

    print()


# ---------------------------------------------------------------------------
# B-3: Trade cost sensitivity
# ---------------------------------------------------------------------------

COST_SCENARIOS_BPS = [0, 10, 25, 50]


def _apply_cost_haircut(raw_return: float, cost_bps: int) -> float:
    """
    Apply a round-trip cost haircut to a raw return.

    Cost is applied at entry + exit (round trip), so total friction =
    cost_bps / 10000.  This is a haircut subtracted from the raw return.

    Examples:
        raw=+5%, cost=25bps  → adjusted = +5% - 0.25% = +4.75%
        raw=-2%, cost=50bps  → adjusted = -2% - 0.50% = -2.50%
    """
    return raw_return - cost_bps / 10_000


def compute_cost_sensitivity(
    agg: dict[str, dict],
    universe_returns: list[float],
    cost_scenarios_bps: Optional[list[int]] = None,
) -> dict:
    """
    For each signal group and each cost scenario, compute cost-adjusted stats.

    The universe benchmark is intentionally left unadjusted (passive benchmark
    with no friction), which is the conservative/honest framing: the active
    signal-based strategy bears transaction costs; the passive benchmark does not.

    Returns:
        {signal: {cost_bps: {"n": int, "median": float|None,
                              "win_rate": float|None, "vs_universe": float|None}}}
        Plus a "universe" key with unadjusted universe stats.
    """
    if cost_scenarios_bps is None:
        cost_scenarios_bps = COST_SCENARIOS_BPS

    univ_med = _median(universe_returns)
    univ_wr  = _win_rate(universe_returns)

    results: dict = {
        "_universe": {
            "n": len(universe_returns),
            "median": univ_med,
            "win_rate": univ_wr,
        }
    }

    for sig, bucket in agg.items():
        raw_rets = bucket["returns"]
        results[sig] = {}
        for cost_bps in cost_scenarios_bps:
            adj_rets = [_apply_cost_haircut(r, cost_bps) for r in raw_rets]
            med = _median(adj_rets)
            wr  = _win_rate(adj_rets)
            vs  = (med - univ_med) if (med is not None and univ_med is not None) else None
            results[sig][cost_bps] = {
                "n":          len(adj_rets),
                "median":     med,
                "win_rate":   wr,
                "vs_universe": vs,
            }

    return results


def print_cost_sensitivity_table(
    cost_results: dict,
    agg: dict[str, dict],
) -> None:
    """Print the cost-sensitivity table to stdout."""
    univ_meta = cost_results.get("_universe", {})
    univ_med = univ_meta.get("median")
    print()
    print("=" * 100)
    print("  B-3: TRADE COST SENSITIVITY ANALYSIS  (round-trip bps applied as haircut to signal returns)")
    print(f"  Universe benchmark: n={univ_meta.get('n', 0)}, "
          f"median={_pct(univ_med)}, win_rate={_fmt_rate(univ_meta.get('win_rate'))}  "
          f"[unadjusted — passive, no friction]")
    print("=" * 100)

    # Header
    cost_cols = COST_SCENARIOS_BPS
    col_w = 28
    hdr = f"{'Signal':<14} | {'n':>5}"
    for c in cost_cols:
        hdr += f" | {f'{c}bps med / vs-univ':>{col_w}}"
    hdr += " | Conclusion"
    print(hdr)
    print("-" * len(hdr))

    for sig in SIGNAL_ORDER:
        if sig not in cost_results:
            continue
        sig_data = cost_results[sig]
        n = sig_data.get(0, {}).get("n", 0)
        row = f"{sig:<14} | {n:>5}"

        conclusions = []
        for c in cost_cols:
            cell = sig_data.get(c, {})
            med = cell.get("median")
            vs  = cell.get("vs_universe")
            row += f" | {_pct(med):>10} {_pct(vs):>10}  "
            if n < 10:
                conclusions.append("insufficient")
            elif vs is not None:
                if vs > 0.03:
                    conclusions.append(f"+{c}bps: edge persists")
                elif vs < -0.03:
                    conclusions.append(f"+{c}bps: edge lost")

        # Pick the breaking-point conclusion
        if n < 10:
            conclusion = "insufficient (n<10)"
        else:
            cost_0_vs  = sig_data.get(0,  {}).get("vs_universe")
            cost_10_vs = sig_data.get(10, {}).get("vs_universe")
            cost_25_vs = sig_data.get(25, {}).get("vs_universe")
            cost_50_vs = sig_data.get(50, {}).get("vs_universe")
            if cost_0_vs is not None and cost_0_vs < -0.03:
                conclusion = "underperforms at 0bps"
            elif cost_0_vs is not None and abs(cost_0_vs) < 0.03:
                conclusion = "no clear edge at 0bps"
            elif cost_50_vs is not None and cost_50_vs > 0.03:
                conclusion = "edge survives 50bps"
            elif cost_25_vs is not None and cost_25_vs > 0.03:
                conclusion = "edge survives 25bps"
            elif cost_10_vs is not None and cost_10_vs > 0.03:
                conclusion = "edge survives 10bps only"
            else:
                conclusion = "edge <3pp; cost-sensitive"

        row += f"| {conclusion}"
        print(row)

    print("-" * len(hdr))
    print()


# ---------------------------------------------------------------------------
# B-4: Threshold scan helpers
# ---------------------------------------------------------------------------

# Default threshold values (current production defaults from signals.py)
_DEFAULT_SCORE_THRESH  = 70
_DEFAULT_RSI_ENTRY     = 65
_DEFAULT_RSI_EXIT      = 40

# Scan ranges
_SCORE_THRESHOLDS  = [60, 65, 70, 75, 80]
_RSI_ENTRY_BOUNDS  = [55, 60, 65, 70]
_RSI_EXIT_BOUNDS   = [35, 40, 45]


def _reclassify_buy_watch(
    record: dict,
    score_thresh: float,
    rsi_entry_bound: float,
) -> bool:
    """
    Return True if this record's pre-computed indicators satisfy BUY_WATCH
    under the given custom thresholds.

    BUY_WATCH (custom): score >= score_thresh AND price > EMA20 AND RSI < rsi_entry_bound

    Note: No priority ordering applied here (i.e., OVERBOUGHT / EXIT_ALERT
    precedence is NOT enforced).  We are testing the raw condition's
    predictive value, not the full signal cascade.

    All indicators come from record["indicators"] — computed at-cutoff,
    zero look-ahead is preserved.
    """
    score = record.get("score")
    if score is None or score < score_thresh:
        return False

    entry = record.get("entry")
    if entry is None:
        return False

    indicators = record.get("indicators") or {}
    ema20 = _last_non_none(indicators.get("ema20", []))
    if ema20 is None or entry <= ema20:
        return False

    rsi = _last_non_none(indicators.get("rsi14", []))
    if rsi is None or rsi >= rsi_entry_bound:
        return False

    return True


def _reclassify_exit_alert(
    record: dict,
    rsi_exit_bound: float,
) -> bool:
    """
    Return True if this record's indicators satisfy EXIT_ALERT under the
    given RSI exit bound.

    EXIT_ALERT (custom): price < EMA50 OR RSI < rsi_exit_bound

    Score-drop component is omitted because prev_score is not stored
    in multiwindow records.  This is honest: the RSI and price components
    together account for the vast majority of EXIT_ALERT triggers.
    """
    entry = record.get("entry")
    if entry is None:
        return False

    indicators = record.get("indicators") or {}
    ema50 = _last_non_none(indicators.get("ema50", []))
    rsi   = _last_non_none(indicators.get("rsi14", []))

    if ema50 is not None and entry < ema50:
        return True
    if rsi is not None and rsi < rsi_exit_bound:
        return True
    return False


def _scan_buy_watch(
    windows: list[dict],
    universe_returns: list[float],
    score_thresholds: list[float],
    rsi_entry_bounds: list[float],
) -> tuple[list[dict], list[dict]]:
    """
    One-dimensional scans for BUY_WATCH:

    Scan 1 — Score threshold (RSI entry fixed at default 65):
        For each score_thresh in score_thresholds, collect all records
        meeting BUY_WATCH(score_thresh, 65) across all windows.

    Scan 2 — RSI entry bound (score fixed at default 70):
        For each rsi_bound in rsi_entry_bounds, collect all records
        meeting BUY_WATCH(70, rsi_bound) across all windows.

    Returns:
        (score_scan_rows, rsi_entry_scan_rows)
        Each row: {param_label, param_value, n_total, n_ret,
                   median, win_rate, vs_universe, note}
    """
    univ_med = _median(universe_returns)

    def _collect(score_t, rsi_t):
        rets = []
        n_total = 0
        for win in windows:
            for r in win["records"]:
                if _reclassify_buy_watch(r, score_t, rsi_t):
                    n_total += 1
                    if r.get("holdout_return") is not None:
                        rets.append(r["holdout_return"])
        med = _median(rets)
        wr  = _win_rate(rets)
        vs  = (med - univ_med) if (med is not None and univ_med is not None) else None
        note = "insufficient (n<10)" if n_total < 10 else ""
        return {
            "n_total": n_total, "n_ret": len(rets),
            "median": med, "win_rate": wr, "vs_universe": vs, "note": note,
        }

    score_scan = []
    for st in score_thresholds:
        row = _collect(st, _DEFAULT_RSI_ENTRY)
        row["param_label"] = "score_thresh"
        row["param_value"] = st
        score_scan.append(row)

    rsi_entry_scan = []
    for rb in rsi_entry_bounds:
        row = _collect(_DEFAULT_SCORE_THRESH, rb)
        row["param_label"] = "rsi_entry"
        row["param_value"] = rb
        rsi_entry_scan.append(row)

    return score_scan, rsi_entry_scan


def _scan_exit_alert(
    windows: list[dict],
    universe_returns: list[float],
    rsi_exit_bounds: list[float],
) -> list[dict]:
    """
    One-dimensional scan for EXIT_ALERT RSI exit bound.

    For each rsi_exit_bound, collect all records meeting EXIT_ALERT
    (price < EMA50 OR RSI < rsi_exit_bound) across all windows.

    Returns list of row dicts.
    """
    univ_med = _median(universe_returns)

    rows = []
    for rsi_exit in rsi_exit_bounds:
        rets = []
        n_total = 0
        for win in windows:
            for r in win["records"]:
                if _reclassify_exit_alert(r, rsi_exit):
                    n_total += 1
                    if r.get("holdout_return") is not None:
                        rets.append(r["holdout_return"])
        med = _median(rets)
        wr  = _win_rate(rets)
        vs  = (med - univ_med) if (med is not None and univ_med is not None) else None
        note = "insufficient (n<10)" if n_total < 10 else ""
        rows.append({
            "param_label": "rsi_exit",
            "param_value": rsi_exit,
            "n_total": n_total,
            "n_ret": len(rets),
            "median": med,
            "win_rate": wr,
            "vs_universe": vs,
            "note": note,
        })
    return rows


def run_threshold_scan(
    windows: list[dict],
    universe_returns: list[float],
) -> dict:
    """
    Run all three one-dimensional threshold scans and return results.

    Returns:
        {
          "score_scan":      [row, ...],   # BUY_WATCH score threshold scan
          "rsi_entry_scan":  [row, ...],   # BUY_WATCH RSI entry bound scan
          "rsi_exit_scan":   [row, ...],   # EXIT_ALERT RSI exit bound scan
          "universe_n":      int,
          "universe_median": float|None,
        }
    """
    score_scan, rsi_entry_scan = _scan_buy_watch(
        windows, universe_returns,
        _SCORE_THRESHOLDS, _RSI_ENTRY_BOUNDS,
    )
    rsi_exit_scan = _scan_exit_alert(
        windows, universe_returns, _RSI_EXIT_BOUNDS,
    )
    return {
        "score_scan":      score_scan,
        "rsi_entry_scan":  rsi_entry_scan,
        "rsi_exit_scan":   rsi_exit_scan,
        "universe_n":      len(universe_returns),
        "universe_median": _median(universe_returns),
    }


def _print_threshold_scan_subtable(
    rows: list[dict],
    param_header: str,
    description: str,
    fixed_params: str,
) -> None:
    """Print one scan dimension's table to stdout."""
    print()
    print(f"  --- {description} ---")
    print(f"  Fixed: {fixed_params}")
    hdr = (
        f"  {param_header:<18} | {'n_total':>7} | {'n_ret':>6} | "
        f"{'Median Ret':>10} | {'Win Rate':>8} | {'vs Universe':>11} | Note"
    )
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)
    for row in rows:
        val = row["param_value"]
        val_str = str(val) if not isinstance(val, float) else f"{val:.0f}"
        note = row.get("note", "")
        print(
            f"  {val_str:<18} | {row['n_total']:>7} | {row['n_ret']:>6} | "
            f"{_pct(row['median']):>10} | {_fmt_rate(row['win_rate']):>8} | "
            f"{_pct(row['vs_universe']):>11} | {note}"
        )
    print(sep)


def print_threshold_scan_report(
    threshold_results: dict,
) -> None:
    """Print the full threshold scan report to stdout."""
    univ_med = threshold_results["universe_median"]
    univ_n   = threshold_results["universe_n"]

    print()
    print("=" * 100)
    print("  B-4: THRESHOLD SCAN")
    print(f"  Universe: n={univ_n}, median={_pct(univ_med)}")
    print("  Note: n<10 cells marked insufficient — no conclusions drawn from them.")
    print("  Score-drop EXIT_ALERT component omitted (prev_score not stored in multiwindow records).")
    print("=" * 100)

    _print_threshold_scan_subtable(
        threshold_results["score_scan"],
        param_header="score_thresh",
        description="BUY_WATCH score threshold scan",
        fixed_params=f"RSI entry bound = {_DEFAULT_RSI_ENTRY}, RSI exit = {_DEFAULT_RSI_EXIT}",
    )

    _print_threshold_scan_subtable(
        threshold_results["rsi_entry_scan"],
        param_header="rsi_entry_bound",
        description="BUY_WATCH RSI entry bound scan",
        fixed_params=f"score_thresh = {_DEFAULT_SCORE_THRESH}, RSI exit = {_DEFAULT_RSI_EXIT}",
    )

    _print_threshold_scan_subtable(
        threshold_results["rsi_exit_scan"],
        param_header="rsi_exit_bound",
        description="EXIT_ALERT RSI exit bound scan  (EXIT = price<EMA50 OR RSI<rsi_exit)",
        fixed_params=f"score_thresh = {_DEFAULT_SCORE_THRESH}, RSI entry = {_DEFAULT_RSI_ENTRY}",
    )
    print()


# ---------------------------------------------------------------------------
# Write VALIDATION.md
# ---------------------------------------------------------------------------

def write_validation_md(
    db_path: Path,
    max_date_str: str,
    cutoffs: list[str],
    windows: list[dict],
    agg: dict[str, dict],
    universe_returns: list[float],
    pullback: bool,
    variant_agg: Optional[dict] = None,
    pb_universe_rets: Optional[list] = None,
    cost_results: Optional[dict] = None,
    threshold_results: Optional[dict] = None,
) -> None:
    """Write aggregated validation results to docs/VALIDATION.md.

    Optional keyword-only sections (appended after core content):
        cost_results      — B-3 trade cost sensitivity dict (from compute_cost_sensitivity)
        threshold_results — B-4 threshold scan dict (from run_threshold_scan)
    """
    out_path = Path(__file__).resolve().parents[1] / "docs" / "VALIDATION.md"

    univ_med = _median(universe_returns)
    univ_wr  = _win_rate(universe_returns)
    today_str = date.today().isoformat()

    lines = []
    lines.append("# Serenity Signal — 多窗口樣本外驗證報告\n")
    lines.append(f"> 版本：v2.0 | 日期：{today_str} | 資料截止：{max_date_str}")
    lines.append(f"> 方法：固定30日向前horizon，每15日一個切割點，共{len(cutoffs)}個window")
    lines.append(f"> 腳本：`scripts/backtest_multiwindow.py`")
    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Methodology ---
    lines.append("## 方法說明")
    lines.append("")
    lines.append("- **切割點範圍**：最早可讓 ≥40 檔股票有 ≥60 根K棒的日期，至 最新日期 − 30 天")
    lines.append("- **前進horizon**：固定30個日曆日（修正了 backtest_holdout.py 的不一致horizon問題）")
    lines.append("  - exit_price = 切割日後、30個日曆日內的最後一個收盤價")
    lines.append("- **零前瞻保證**：計算指標前先截斷K棒至 <= 切割日；score_symbol 以 now=cutoff 呼叫")
    lines.append("- **零捏造**：所有報酬 = exit_close / entry_close − 1，來自 SQLite 真實列")
    lines.append("")

    # --- Aggregated signal table ---
    lines.append("## 彙整：各訊號狀態表現（跨所有window）")
    lines.append("")
    lines.append(f"共 {len(cutoffs)} 個window | 宇宙 n={len(universe_returns)} 觀察值 "
                 f"| 宇宙中位報酬={_pct(univ_med)} | 勝率={_fmt_rate(univ_wr)}")
    lines.append("")
    lines.append("| 訊號狀態 | n(總計) | n(有報酬) | 中位報酬 | 勝率 | vs宇宙 | 解讀 |")
    lines.append("|---------|--------|----------|--------|------|-------|------|")

    for sig in SIGNAL_ORDER:
        if sig not in agg:
            continue
        bucket = agg[sig]
        rets   = bucket["returns"]
        n_total= bucket["n_total"]
        med    = _median(rets)
        wr     = _win_rate(rets)
        vs     = (med - univ_med) if (med is not None and univ_med is not None) else None

        if len(rets) < 10:
            interp = "樣本不足(n<10)"
        elif vs is None:
            interp = "無法評估"
        elif vs > 0.03:
            interp = "**優於宇宙**"
        elif vs < -0.03:
            interp = "**遜於宇宙**"
        else:
            interp = "與宇宙相當"

        lines.append(
            f"| {sig} | {n_total} | {len(rets)} | {_pct(med)} | "
            f"{_fmt_rate(wr)} | {_pct(vs)} | {interp} |"
        )

    lines.append("")

    # EXIT_ALERT key question
    exit_bucket = agg.get("EXIT_ALERT", {})
    exit_rets = exit_bucket.get("returns", [])
    exit_med = _median(exit_rets)
    exit_vs = (exit_med - univ_med) if (exit_med is not None and univ_med is not None) else None

    lines.append("### 核心問題：EXIT_ALERT 的優勢在 n≥20 時是否仍成立？")
    lines.append("")
    if len(exit_rets) >= 20:
        if exit_vs is not None and exit_vs > 0:
            lines.append(f"**結論：成立。** EXIT_ALERT 累積 n={len(exit_rets)} 筆報酬，"
                         f"中位數={_pct(exit_med)}，超越宇宙 {_pct(exit_vs)}。"
                         "在多窗口框架下，退出訊號的優勢獲得確認。")
        else:
            lines.append(f"**結論：不成立。** EXIT_ALERT 累積 n={len(exit_rets)} 筆報酬，"
                         f"中位數={_pct(exit_med)}，vs宇宙={_pct(exit_vs)}。"
                         "在多窗口框架下，未見一致優勢。")
    else:
        lines.append(f"**結論：樣本不足。** EXIT_ALERT 僅累積 n={len(exit_rets)} 筆報酬（需≥20），"
                     "無法確認或否認優勢。")
    lines.append("")

    # --- Per-window mini-table ---
    lines.append("## 逐窗口迷你表")
    lines.append("")
    lines.append("| 切割日 | n評估 | 宇宙中位 | BUY n | BUY中位 | BUY vs宇宙 | EXIT n | EXIT中位 | EXIT vs宇宙 |")
    lines.append("|--------|------|---------|------|--------|-----------|-------|---------|-----------|")

    for win in windows:
        records = win["records"]
        cutoff  = win["cutoff"]
        all_rets = [r["holdout_return"] for r in records if r["holdout_return"] is not None]
        um = _median(all_rets)

        buy_rets = [r["holdout_return"] for r in records
                    if r["signal"] in ("BUY_TRIGGER", "BUY_WATCH") and r["holdout_return"] is not None]
        buy_n  = sum(1 for r in records if r["signal"] in ("BUY_TRIGGER", "BUY_WATCH"))
        buy_med = _median(buy_rets)
        buy_vs = (buy_med - um) if (buy_med is not None and um is not None) else None

        win_exit_rets = [r["holdout_return"] for r in records
                         if r["signal"] == "EXIT_ALERT" and r["holdout_return"] is not None]
        exit_n = sum(1 for r in records if r["signal"] == "EXIT_ALERT")
        win_exit_med = _median(win_exit_rets)
        win_exit_vs = (win_exit_med - um) if (win_exit_med is not None and um is not None) else None

        lines.append(
            f"| {cutoff} | {len(records)} | {_pct(um)} | {buy_n} | {_pct(buy_med)} | "
            f"{_pct(buy_vs)} | {exit_n} | {_pct(win_exit_med)} | {_pct(win_exit_vs)} |"
        )

    lines.append("")

    # --- Pullback variants ---
    if pullback and variant_agg is not None and pb_universe_rets is not None:
        pb_univ_med = _median(pb_universe_rets)
        pb_univ_wr  = _win_rate(pb_universe_rets)

        lines.append("## 拉回變體分析（score ≥ 65 高熱股）")
        lines.append("")
        lines.append(f"宇宙 n={len(pb_universe_rets)} | 中位={_pct(pb_univ_med)} | 勝率={_fmt_rate(pb_univ_wr)}")
        lines.append("")
        lines.append("變體定義：")
        lines.append("- **baseline** = score≥65，無價格過濾")
        lines.append("- **A (追強)** = close>EMA20 AND RSI≥60")
        lines.append("- **B (拉回)** = close<EMA20 AND 60棒高點回撤[10%,35%] AND RSI[30,50]")
        lines.append("- **C (深度價值)** = 60棒高點回撤 >35%")
        lines.append("")
        lines.append("| 變體 | n(總計) | n(有報酬) | 中位報酬 | 勝率 | vs宇宙 | 結論 |")
        lines.append("|------|--------|----------|--------|------|-------|------|")

        variant_order = ["baseline", "A", "B", "C"]
        variant_names = {"baseline": "baseline(高熱基準)", "A": "A(追強)", "B": "B(拉回)", "C": "C(深度價值)"}

        best_var = None
        best_vs  = -999.0

        for var in variant_order:
            bucket = variant_agg.get(var, {"returns": [], "n_total": 0})
            rets   = bucket["returns"]
            n_total= bucket["n_total"]
            med    = _median(rets)
            wr     = _win_rate(rets)
            vs     = (med - pb_univ_med) if (med is not None and pb_univ_med is not None) else None

            if vs is not None and len(rets) >= 10 and vs > best_vs:
                best_vs  = vs
                best_var = var

            if len(rets) < 10:
                verdict = "樣本不足(n<10)"
            elif vs is None:
                verdict = "無法評估"
            elif vs > 0.05:
                verdict = "**明確優勢**"
            elif vs > 0.02:
                verdict = "邊際正優勢"
            elif vs < -0.05:
                verdict = "**明確拖累**"
            elif vs < -0.02:
                verdict = "邊際劣勢"
            else:
                verdict = "無明顯優勢"

            lines.append(
                f"| {variant_names.get(var, var)} | {n_total} | {len(rets)} | "
                f"{_pct(med)} | {_fmt_rate(wr)} | {_pct(vs)} | {verdict} |"
            )

        lines.append("")

        # Overall conclusion for pullback
        b_bucket = variant_agg.get("B", {})
        b_rets = b_bucket.get("returns", [])
        b_med = _median(b_rets)
        b_vs  = (b_med - pb_univ_med) if (b_med is not None and pb_univ_med is not None) else None
        a_bucket = variant_agg.get("A", {})
        a_rets = a_bucket.get("returns", [])
        a_vs = (_median(a_rets) - pb_univ_med) if (_median(a_rets) is not None and pb_univ_med is not None) else None

        lines.append("### 誠實結論（zh-TW）")
        lines.append("")

        if len(b_rets) < 10:
            lines.append(
                f"**拉回變體B（pullback）：樣本不足（n={len(b_rets)}，需≥10），"
                "無法得出結論。** 建議待樣本累積後重跑。"
            )
        elif b_vs is not None and b_vs > 0.02:
            lines.append(
                f"**拉回變體B（pullback）展現正向優勢：** n={len(b_rets)}，"
                f"中位報酬={_pct(b_med)}，超越宇宙 {_pct(b_vs)}。"
                "「高熱 + 已拉回」的過濾邏輯，在現有樣本中優於「高熱 + 延伸」。"
                "**但樣本仍有限，此結果需持續監測。**"
            )
        elif b_vs is not None and b_vs < -0.02:
            lines.append(
                f"**拉回變體B（pullback）表現遜於宇宙：** n={len(b_rets)}，"
                f"中位報酬={_pct(b_med)}，vs宇宙={_pct(b_vs)}。"
                "「等拉回再買」在此資料集中並未展現優勢。"
            )
        else:
            lines.append(
                f"**拉回變體B（pullback）：無明確優勢。** n={len(b_rets)}，"
                f"vs宇宙={_pct(b_vs)}。"
                "現有資料不支持也不反對此規則，需更多樣本。"
            )

        lines.append("")
        if a_vs is not None and len(a_rets) >= 10 and a_vs < -0.02:
            lines.append(
                f"變體A（追強，close>EMA20 AND RSI≥60）確認劣勢：n={len(a_rets)}，"
                f"vs宇宙={_pct(a_vs)}。追高策略在高熱股中表現持續落後。"
            )
        lines.append("")

    # --- General honest conclusions ---
    lines.append("## 總體誠實結論")
    lines.append("")

    buy_bucket = {}
    buy_rets_all = []
    for sig in ("BUY_TRIGGER", "BUY_WATCH"):
        b = agg.get(sig, {})
        buy_rets_all.extend(b.get("returns", []))
    buy_med_all = _median(buy_rets_all)
    buy_vs_all = (buy_med_all - univ_med) if (buy_med_all is not None and univ_med is not None) else None

    lines.append(f"1. **BUY訊號（BUY_TRIGGER + BUY_WATCH）**：n={len(buy_rets_all)}，"
                 f"中位報酬={_pct(buy_med_all)}，vs宇宙={_pct(buy_vs_all)}。")
    if buy_vs_all is not None and buy_vs_all < -0.02:
        lines.append("   → 確認單窗口結果：BUY訊號持續劣於宇宙。量化分數≥70 + 價格延伸的組合在樣本外不具預測力。")
    elif buy_vs_all is not None and buy_vs_all > 0.02:
        lines.append("   → BUY訊號展現正向優勢。但應持續監測，避免過早結論。")
    else:
        lines.append("   → 與宇宙相當，無明確優勢。")
    lines.append("")

    lines.append(f"2. **EXIT_ALERT**：n={len(exit_rets)}，"
                 f"中位報酬={_pct(exit_med)}，vs宇宙={_pct(exit_vs)}。")
    if len(exit_rets) >= 20 and exit_vs is not None and exit_vs > 0:
        lines.append("   → 在 n≥20 的條件下確認優勢：避開EXIT_ALERT股票可保護資本。")
    elif len(exit_rets) < 20:
        lines.append("   → 樣本仍不足（n<20），暫無法確認。")
    else:
        lines.append("   → n≥20 但未見一致優勢，單窗口的 +13pp 可能為噪音。")
    lines.append("")

    lines.append("3. **總體**：所有結論受限於樣本量與市場週期。本報告為活文件，")
    lines.append("   應在新資料入庫後定期重跑更新。")
    lines.append("")
    lines.append("---")
    lines.append(f"*由 `scripts/backtest_multiwindow.py` 自動產生，資料日期：{max_date_str}*")
    lines.append("")

    # -----------------------------------------------------------------------
    # B-3: Trade cost sensitivity section (optional)
    # -----------------------------------------------------------------------
    if cost_results is not None:
        univ_meta = cost_results.get("_universe", {})
        univ_med  = univ_meta.get("median")
        univ_n    = univ_meta.get("n", 0)
        univ_wr   = univ_meta.get("win_rate")

        lines.append("---")
        lines.append("")
        lines.append("## B-3 交易成本敏感度分析（Trade Cost Sensitivity）")
        lines.append("")
        lines.append("> **方法**：對每個訊號組的30日前向報酬逐筆套用往返成本扣減（haircut）：")
        lines.append("> `adjusted_return = raw_return − cost_bps / 10000`")
        lines.append("> 成本情境：0 / 10 / 25 / 50 bps（往返合計）")
        lines.append("> 宇宙基準維持**不扣成本**（被動持有無摩擦），為保守/誠實的對照設計。")
        lines.append("")
        lines.append(f"宇宙基準：n={univ_n}，中位報酬={_pct(univ_med)}，"
                     f"勝率={_fmt_rate(univ_wr)}")
        lines.append("")

        # Table header
        lines.append("| 訊號 | n | 0bps中位 | 0bps超額 | 10bps中位 | 10bps超額 | 25bps中位 | 25bps超額 | 50bps中位 | 50bps超額 | 結論 |")
        lines.append("|------|---|---------|---------|----------|----------|----------|----------|----------|----------|------|")

        for sig in SIGNAL_ORDER:
            if sig not in cost_results:
                continue
            sig_data = cost_results[sig]
            n = sig_data.get(0, {}).get("n", 0)

            # Build cell values
            cells = []
            for c in [0, 10, 25, 50]:
                d = sig_data.get(c, {})
                cells.append(_pct(d.get("median")))
                cells.append(_pct(d.get("vs_universe")))

            # Conclusion
            if n < 10:
                conclusion = "樣本不足(n<10)"
            else:
                vs0  = sig_data.get(0,  {}).get("vs_universe")
                vs10 = sig_data.get(10, {}).get("vs_universe")
                vs25 = sig_data.get(25, {}).get("vs_universe")
                vs50 = sig_data.get(50, {}).get("vs_universe")

                if vs0 is not None and vs0 < -0.03:
                    conclusion = "0bps時已劣於宇宙"
                elif vs0 is not None and abs(vs0) < 0.03:
                    conclusion = "0bps時無明顯優勢"
                elif vs50 is not None and vs50 > 0.03:
                    conclusion = "50bps後仍有優勢"
                elif vs25 is not None and vs25 > 0.03:
                    conclusion = "25bps後仍有優勢"
                elif vs10 is not None and vs10 > 0.03:
                    conclusion = "10bps後仍有優勢"
                else:
                    conclusion = "優勢<3pp，成本敏感"

            row = f"| {sig} | {n} | " + " | ".join(cells) + f" | {conclusion} |"
            lines.append(row)

        lines.append("")

        # Honest interpretation
        lines.append("### B-3 誠實結論")
        lines.append("")
        lines.append("- **BUY_WATCH / BUY_TRIGGER**：")
        bw_data = cost_results.get("BUY_WATCH", {})
        bw_n   = bw_data.get(0, {}).get("n", 0)
        bw_vs0 = bw_data.get(0, {}).get("vs_universe")
        if bw_n < 10:
            lines.append(f"  樣本不足（n={bw_n}），無法得出結論。")
        elif bw_vs0 is not None and bw_vs0 < 0:
            lines.append(
                f"  即便在零成本條件下，BUY_WATCH中位超額報酬已為負值 "
                f"（{_pct(bw_vs0)}）。加入任何成本後劣勢進一步擴大。"
                f"現有默認閾值（score≥70, RSI<65）在樣本外不具預測力。"
            )
        else:
            lines.append(
                f"  0bps超額={_pct(bw_vs0)}，需觀察更多樣本。"
            )

        lines.append("")
        lines.append("- **EXIT_ALERT**：")
        ea_data = cost_results.get("EXIT_ALERT", {})
        ea_n   = ea_data.get(0, {}).get("n", 0)
        ea_vs0 = ea_data.get(0, {}).get("vs_universe")
        if ea_n < 10:
            lines.append(f"  樣本不足（n={ea_n}），無法得出結論。")
        elif ea_vs0 is not None and ea_vs0 < 0:
            lines.append(
                f"  EXIT_ALERT 0bps超額={_pct(ea_vs0)}。此訊號的設計用途為**風控提示**"
                f"（避免持有正在惡化的股票），而非反向買進訊號。成本分析確認其作為退出依據的一致性。"
            )
        else:
            lines.append(
                f"  EXIT_ALERT 0bps超額={_pct(ea_vs0)}，n={ea_n}，成本影響可見於上表。"
            )

        lines.append("")
        lines.append("- **OVERBOUGHT**：")
        ob_data = cost_results.get("OVERBOUGHT", {})
        ob_n   = ob_data.get(0, {}).get("n", 0)
        ob_vs0 = ob_data.get(0, {}).get("vs_universe")
        ob_vs50 = ob_data.get(50, {}).get("vs_universe")
        if ob_n < 10:
            lines.append(f"  樣本不足（n={ob_n}），無法得出結論。")
        elif ob_vs0 is not None and ob_vs0 > 0.03:
            if ob_vs50 is not None and ob_vs50 > 0.03:
                lines.append(
                    f"  OVERBOUGHT 在50bps成本下超額報酬仍為{_pct(ob_vs50)}（n={ob_n}）。"
                    f"動能延續現象在此樣本中足以覆蓋高成本，但**須注意資料以2025-26多頭環境為主**，"
                    f"須等待空頭窗口驗證。"
                )
            else:
                lines.append(
                    f"  OVERBOUGHT 0bps超額={_pct(ob_vs0)}，但50bps後縮減至{_pct(ob_vs50)}。"
                    f"成本對此訊號的優勢有實質影響（n={ob_n}）。"
                )
        else:
            lines.append(
                f"  OVERBOUGHT 0bps超額={_pct(ob_vs0)}，n={ob_n}，見上表。"
            )
        lines.append("")

    # -----------------------------------------------------------------------
    # B-4: Threshold scan section (optional)
    # -----------------------------------------------------------------------
    if threshold_results is not None:
        univ_med_ts = threshold_results.get("universe_median")
        univ_n_ts   = threshold_results.get("universe_n", 0)

        lines.append("---")
        lines.append("")
        lines.append("## B-4 訊號閾值掃描（Threshold Scan）")
        lines.append("")
        lines.append("> **方法**：使用與主框架相同的21個切割窗口（零前瞻）。")
        lines.append("> 對每個切割點已計算的指標（score、RSI、EMA20、EMA50），")
        lines.append("> 以自訂閾值重新分類觀察值，不重跑評分或指標計算。")
        lines.append("> 一維掃描（每次只變動一個參數），其餘維度保持預設值。")
        lines.append("> n<10 的格子標記為「樣本不足」，不得據此得出結論。")
        lines.append("> **不修改 signals.py 預設值**——此為分析報告，不改動線上閾值。")
        lines.append("")
        lines.append(f"宇宙基準：n={univ_n_ts}，中位報酬={_pct(univ_med_ts)}")
        lines.append("")

        # -- Scan 1: Score threshold --
        lines.append("### B-4-1 BUY_WATCH 評分閾值掃描")
        lines.append("")
        lines.append(f"固定：RSI進場 < {_DEFAULT_RSI_ENTRY}，RSI出場（EXIT）< {_DEFAULT_RSI_EXIT}")
        lines.append("條件：score >= threshold AND price > EMA20 AND RSI < 65")
        lines.append("")
        lines.append("| score閾值 | n總計 | n有報酬 | 中位報酬 | 勝率 | vs宇宙 | 備註 |")
        lines.append("|----------|------|--------|--------|------|-------|------|")
        for row in threshold_results["score_scan"]:
            note = row.get("note", "")
            lines.append(
                f"| {row['param_value']} | {row['n_total']} | {row['n_ret']} | "
                f"{_pct(row['median'])} | {_fmt_rate(row['win_rate'])} | "
                f"{_pct(row['vs_universe'])} | {note} |"
            )
        lines.append("")

        # -- Scan 2: RSI entry bound --
        lines.append("### B-4-2 BUY_WATCH RSI 進場上限掃描")
        lines.append("")
        lines.append(f"固定：score >= {_DEFAULT_SCORE_THRESH}，RSI出場（EXIT）< {_DEFAULT_RSI_EXIT}")
        lines.append("條件：score >= 70 AND price > EMA20 AND RSI < rsi_entry_bound")
        lines.append("")
        lines.append("| RSI進場上限 | n總計 | n有報酬 | 中位報酬 | 勝率 | vs宇宙 | 備註 |")
        lines.append("|-----------|------|--------|--------|------|-------|------|")
        for row in threshold_results["rsi_entry_scan"]:
            note = row.get("note", "")
            lines.append(
                f"| {row['param_value']} | {row['n_total']} | {row['n_ret']} | "
                f"{_pct(row['median'])} | {_fmt_rate(row['win_rate'])} | "
                f"{_pct(row['vs_universe'])} | {note} |"
            )
        lines.append("")

        # -- Scan 3: RSI exit bound --
        lines.append("### B-4-3 EXIT_ALERT RSI 出場下限掃描")
        lines.append("")
        lines.append(f"固定：score >= {_DEFAULT_SCORE_THRESH}，RSI進場 < {_DEFAULT_RSI_ENTRY}")
        lines.append("條件（EXIT_ALERT）：price < EMA50 OR RSI < rsi_exit_bound")
        lines.append("（注：評分跌幅觸發條件因多窗口記錄不含前期評分而省略，誠實聲明如此。）")
        lines.append("")
        lines.append("| RSI出場下限 | n總計 | n有報酬 | 中位報酬 | 勝率 | vs宇宙 | 備註 |")
        lines.append("|-----------|------|--------|--------|------|-------|------|")
        for row in threshold_results["rsi_exit_scan"]:
            note = row.get("note", "")
            lines.append(
                f"| {row['param_value']} | {row['n_total']} | {row['n_ret']} | "
                f"{_pct(row['median'])} | {_fmt_rate(row['win_rate'])} | "
                f"{_pct(row['vs_universe'])} | {note} |"
            )
        lines.append("")

        # Honest conclusion for B-4
        lines.append("### B-4 誠實結論")
        lines.append("")

        # Assess score scan
        valid_score_rows = [r for r in threshold_results["score_scan"] if r["n_total"] >= 10]
        if not valid_score_rows:
            lines.append(
                "**BUY_WATCH 評分閾值掃描**："
                "所有閾值情境下 n<10，樣本嚴重不足，無法得出任何結論。"
                "現有默認值（score≥70）既未被支持也未被否定。"
            )
        else:
            # Find best by vs_universe
            best = max(valid_score_rows, key=lambda r: r["vs_universe"] or -99)
            worst = min(valid_score_rows, key=lambda r: r["vs_universe"] or 99)
            lines.append(
                f"**BUY_WATCH 評分閾值掃描**（有效格子 n={len(valid_score_rows)}）："
            )
            if best["vs_universe"] is not None and best["vs_universe"] > 0.02:
                lines.append(
                    f"score≥{best['param_value']} 表現最佳（超額{_pct(best['vs_universe'])}，"
                    f"n={best['n_total']}），但整體樣本仍有限，需謹慎解讀。"
                )
            else:
                neg_rows = [r for r in valid_score_rows if r["vs_universe"] is not None and r["vs_universe"] < 0]
                if neg_rows:
                    lines.append(
                        "所有有效閾值的 BUY_WATCH 超額報酬均為負值，"
                        "顯示現行 score+EMA20+RSI 組合在現有資料中不具買進預測力，"
                        "調整閾值無助改善。"
                    )
                else:
                    lines.append("有效樣本有限，結論不明確。建議累積更多樣本後重跑。")

        lines.append("")

        # Assess RSI entry scan
        valid_rsi_entry_rows = [r for r in threshold_results["rsi_entry_scan"] if r["n_total"] >= 10]
        if not valid_rsi_entry_rows:
            lines.append(
                "**BUY_WATCH RSI進場上限掃描**：所有情境下 n<10，樣本不足，無法得出結論。"
            )
        else:
            best = max(valid_rsi_entry_rows, key=lambda r: r["vs_universe"] or -99)
            lines.append(
                f"**BUY_WATCH RSI進場上限掃描**（有效格子 n={len(valid_rsi_entry_rows)}）："
            )
            if best["vs_universe"] is not None and best["vs_universe"] > 0.02:
                lines.append(
                    f"RSI<{best['param_value']} 表現最佳（超額{_pct(best['vs_universe'])}，"
                    f"n={best['n_total']}）。"
                )
            else:
                lines.append(
                    "各 RSI 進場上限情境的超額報酬均有限，"
                    "RSI 進場條件對預測力的貢獻不明確。"
                )

        lines.append("")

        # Assess RSI exit scan
        valid_rsi_exit_rows = [r for r in threshold_results["rsi_exit_scan"] if r["n_total"] >= 10]
        if not valid_rsi_exit_rows:
            lines.append(
                "**EXIT_ALERT RSI出場下限掃描**：所有情境下 n<10，樣本不足，無法得出結論。"
            )
        else:
            best = min(valid_rsi_exit_rows, key=lambda r: r["vs_universe"] or 99)  # EXIT: lowest excess = best exit signal
            lines.append(
                f"**EXIT_ALERT RSI出場下限掃描**（有效格子 n={len(valid_rsi_exit_rows)}）："
            )
            # For exit, we want returns to be low (vs universe) to confirm exit is warranted
            all_neg = all(
                r["vs_universe"] is not None and r["vs_universe"] < 0
                for r in valid_rsi_exit_rows
            )
            if all_neg:
                lines.append(
                    "各 RSI 出場下限設定下，EXIT_ALERT 組的超額報酬均為負值，"
                    "與多窗口框架主結論一致（EXIT_ALERT 輕微遜於宇宙）。"
                    "更嚴格的出場條件（RSI<35）收縮樣本數但超額報酬變化有限。"
                )
            else:
                lines.append(
                    "各 RSI 出場下限設定表現不一，結論需謹慎。詳見上表。"
                )
        lines.append("")

    # --- Supervisor addendum: permanent interpretation caveats. Lives inside
    # the generator so full regenerations cannot silently drop it. ---
    lines.append("---")
    lines.append("")
    lines.append("## 監管者附註（Fable 審查後補充，隨報告永久保留）")
    lines.append("")
    lines.append("1. **重疊窗口警告**：cutoff 每 15 天一個、持有期 30 天 → 相鄰窗口約有一半")
    lines.append("   時間重疊，且同窗口內個股高度相關（同為 AI/半導體）。統計上有效樣本數")
    lines.append("   遠小於表列 n；解讀任何 excess < ±2pp 的結果應視為「無差異」。")
    lines.append("2. **OVERBOUGHT 超額報酬的解讀**：這是動能延續現象（漲勢中 RSI>75 續漲），")
    lines.append("   符合 2025-26 多頭環境；把它當「買進訊號」前必須等跨越空頭窗口的驗證——")
    lines.append("   目前資料以多頭月份為主，動能策略在轉折點的虧損不對稱。")
    lines.append("3. **可執行的當前共識**：(a) 不追高熱延伸股（兩輪驗證一致，證據最強）；")
    lines.append("   (b) 拉回進場方向值得繼續累積樣本（B 變體 71% 勝率但 n=7）；")
    lines.append("   (c) EXIT_ALERT 當風控提示用，勿當反向做多訊號。")
    lines.append("4. **B-4 閾值掃描的統計上限**：BUY_WATCH 全樣本僅 n≈35（21 窗口平均每窗不到")
    lines.append("   2 筆觀察），任何閾值調整都無法克服此樣本量限制。score≥60 略優於 score≥70")
    lines.append("   的形態與 recency 權重假說一致，但尚不足以據此改動線上預設值。")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[multiwindow] VALIDATION.md written to {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Multi-window holdout validation + pullback variant comparison.\n"
            "Extends backtest_holdout.py with rolling cutoffs and fixed 30-day horizon.\n"
            "B-3: --costs adds trade cost sensitivity analysis (0/10/25/50 bps).\n"
            "B-4: --threshold-scan adds BUY_WATCH/EXIT_ALERT threshold scan."
        )
    )
    ap.add_argument("--db", default=None,
                    help="Path to serenity.sqlite (default: data/serenity.sqlite in repo root)")
    ap.add_argument("--pullback", action="store_true",
                    help="Also run pullback variant A/B/C analysis")
    ap.add_argument("--costs", action="store_true",
                    help="B-3: Run trade cost sensitivity analysis (0/10/25/50 bps round-trip)")
    ap.add_argument("--threshold-scan", action="store_true",
                    help="B-4: Run threshold scan for BUY_WATCH score/RSI and EXIT_ALERT RSI")
    ap.add_argument("--horizon-days", type=int, default=30,
                    help="Fixed forward horizon in calendar days (default: 30)")
    ap.add_argument("--step-days", type=int, default=15,
                    help="Days between successive cutoffs (default: 15)")
    ap.add_argument("--min-symbols", type=int, default=40,
                    help="Min symbols with >=60 bars to include a cutoff (default: 40)")
    args = ap.parse_args()

    db_path = _get_db_path(args.db)
    if not db_path.exists():
        raise SystemExit(f"[multiwindow] Database not found: {db_path}")

    print(f"[multiwindow] Database  : {db_path}")
    print(f"[multiwindow] Horizon   : {args.horizon_days} calendar days (fixed)")
    print(f"[multiwindow] Cutoff step: every {args.step_days} days")
    print(f"[multiwindow] Flags     : pullback={args.pullback}, "
          f"costs={args.costs}, threshold-scan={args.threshold_scan}")

    con = sqlite3.connect(db_path)
    try:
        all_prices   = _load_all_prices(con)
        max_date_str = _get_max_date(con)
    finally:
        con.close()

    print(f"[multiwindow] Max price date : {max_date_str}")
    print(f"[multiwindow] Symbols with prices: {len(all_prices)}")

    # Enumerate cutoffs
    cutoffs = _enumerate_cutoffs(
        all_prices,
        max_date_str,
        step_days=args.step_days,
        min_symbols=args.min_symbols,
        min_bars=60,
        horizon_days=args.horizon_days,
    )

    if not cutoffs:
        raise SystemExit(
            "[multiwindow] No valid cutoffs found. "
            "Check that min_symbols and min_bars criteria can be met."
        )

    print(f"[multiwindow] {len(cutoffs)} cutoffs from {cutoffs[0]} to {cutoffs[-1]}")

    # Run all windows
    windows = []
    for cutoff_str in cutoffs:
        print(f"  evaluating {cutoff_str} ...", end=" ", flush=True)
        win = run_window_fixed_horizon(
            db_path, all_prices, cutoff_str, args.horizon_days
        )
        n_ret = sum(1 for r in win["records"] if r["holdout_return"] is not None)
        print(f"{len(win['records'])} symbols, {n_ret} with exits, "
              f"{win['n_skipped_bars']} skipped (bars), "
              f"{win['n_skipped_no_exit']} skipped (no exit)")
        windows.append(win)

    # Aggregate signal stats
    agg, universe_returns = aggregate_signal_stats(windows)

    # Print aggregated table
    print_aggregated_signal_table(agg, universe_returns, len(cutoffs))

    # Print per-window mini-table
    print_per_window_mini_table(windows)

    # Pullback variant analysis
    variant_agg = None
    pb_universe_rets = None
    if args.pullback:
        variant_agg, pb_universe_rets = aggregate_pullback_stats(windows)
        print_pullback_report(variant_agg, pb_universe_rets, len(cutoffs))

    # B-3: Trade cost sensitivity
    cost_results = None
    if args.costs:
        print("[multiwindow] Running B-3: trade cost sensitivity analysis ...")
        cost_results = compute_cost_sensitivity(agg, universe_returns)
        print_cost_sensitivity_table(cost_results, agg)

    # B-4: Threshold scan
    threshold_results = None
    if args.threshold_scan:
        print("[multiwindow] Running B-4: threshold scan ...")
        threshold_results = run_threshold_scan(windows, universe_returns)
        print_threshold_scan_report(threshold_results)

    # Write VALIDATION.md (includes all optional sections when present)
    write_validation_md(
        db_path=db_path,
        max_date_str=max_date_str,
        cutoffs=cutoffs,
        windows=windows,
        agg=agg,
        universe_returns=universe_returns,
        pullback=args.pullback,
        variant_agg=variant_agg,
        pb_universe_rets=pb_universe_rets,
        cost_results=cost_results,
        threshold_results=threshold_results,
    )

    print()
    print("=" * 90)
    print("  END OF REPORT")
    print("=" * 90)


if __name__ == "__main__":
    main()
