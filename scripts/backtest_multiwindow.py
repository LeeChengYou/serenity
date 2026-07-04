#!/usr/bin/env python3
"""
Multi-window holdout validation + pullback variant comparison — R-1 extension.

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

ZERO LOOK-AHEAD GUARANTEE
  Bars are truncated to <= cutoff BEFORE any indicator or score computation.
  evaluate_symbol_at_cutoff (imported from backtest_holdout.py) passes
  now=cutoff_dt to score_symbol, ensuring mentions are also point-in-time.

ZERO FABRICATED DATA
  Every return = exit_close / entry_close - 1 from real SQLite rows.
  Insufficient samples are stated as "insufficient", never forced.

Usage:
    python scripts/backtest_multiwindow.py [--db PATH] [--pullback]
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
) -> None:
    """Write aggregated validation results to docs/VALIDATION.md."""
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

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[multiwindow] VALIDATION.md written to {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Multi-window holdout validation + pullback variant comparison.\n"
            "Extends backtest_holdout.py with rolling cutoffs and fixed 30-day horizon."
        )
    )
    ap.add_argument("--db", default=None,
                    help="Path to serenity.sqlite (default: data/serenity.sqlite in repo root)")
    ap.add_argument("--pullback", action="store_true",
                    help="Also run pullback variant A/B/C analysis")
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

    # Write VALIDATION.md
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
    )

    print()
    print("=" * 90)
    print("  END OF REPORT")
    print("=" * 90)


if __name__ == "__main__":
    main()
