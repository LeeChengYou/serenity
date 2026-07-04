#!/usr/bin/env python3
"""
Temporal holdout validation for Serenity Signal — R-1 + R-2.

Implements:
  - 30-day out-of-sample holdout (R-1, fixes D-1 / D-2)
  - Cross-sectional quintile test at 3 historical windows (fixes D-1)
  - Leave-one-out component weight scan (R-2, fixes D-3)

ZERO LOOK-AHEAD GUARANTEE
  Bar lists are truncated to <= cutoff BEFORE indicators/signals are computed.
  score_symbol is called with now=cutoff_end_of_day.
  news_sentiment rows with published_at > cutoff are excluded; at the
  30-day-ago cutoff all StockTwits rows in the DB post-date the cutoff, so
  sentiment is passed as None — that is the honest answer.

ZERO FABRICATED DATA
  Every return = exit_close / entry_close - 1 using real stored SQLite rows.
  Symbols with insufficient samples are marked "insufficient", never padded.

Usage:
    python scripts/backtest_holdout.py [--db PATH] [--holdout-days N]
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Module-level imports from existing single-source-of-truth modules
# ---------------------------------------------------------------------------

def _load_quant_scorer():
    """Import score_symbol from the serenity-stock-scorer skill by path."""
    scorer_path = (
        Path(__file__).resolve().parents[1]
        / "skills" / "serenity-stock-scorer" / "scripts" / "score_serenity_stock.py"
    )
    spec = importlib.util.spec_from_file_location("score_serenity_stock", scorer_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.score_symbol


def _load_indicators_module():
    """Import compute_all from scripts/indicators.py."""
    ind_path = Path(__file__).resolve().parent / "indicators.py"
    spec = importlib.util.spec_from_file_location("indicators", ind_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.compute_all


def _load_signals_module():
    """Import evaluate_signal from scripts/signals.py."""
    sig_path = Path(__file__).resolve().parent / "signals.py"
    spec = importlib.util.spec_from_file_location("signals", sig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.evaluate_signal


_score_symbol = _load_quant_scorer()
_compute_all = _load_indicators_module()
_evaluate_signal = _load_signals_module()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db_path(explicit: Optional[str]) -> Path:
    """Resolve database path: explicit arg → data/serenity.sqlite."""
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parents[1] / "data" / "serenity.sqlite"


def _load_all_prices(con: sqlite3.Connection) -> dict[str, list[dict]]:
    """
    Load full price history as {symbol: [bar_dict, ...]} oldest-first.

    Each bar_dict has keys: date, open, high, low, close, volume.
    Rows with NULL or non-positive close are excluded.
    """
    rows = con.execute(
        "SELECT symbol, date, open, high, low, close, volume "
        "FROM prices "
        "WHERE close IS NOT NULL AND close > 0 "
        "ORDER BY symbol, date"
    ).fetchall()

    prices: dict[str, list[dict]] = {}
    for sym, date, open_, high, low, close, vol in rows:
        bar = {
            "date": date,
            "open": float(open_) if open_ is not None else None,
            "high": float(high) if high is not None else None,
            "low": float(low) if low is not None else None,
            "close": float(close),
            "volume": float(vol) if vol is not None else None,
        }
        prices.setdefault(sym, []).append(bar)
    return prices


def _get_max_date(con: sqlite3.Connection) -> str:
    """Return the latest date string (YYYY-MM-DD) in the prices table."""
    row = con.execute("SELECT MAX(date) FROM prices WHERE close > 0").fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Point-in-time evaluation: score + indicators + signal for one symbol
# at one cutoff date
# ---------------------------------------------------------------------------

ADDITIVE_COMPONENTS = [
    "frequency", "recency", "persistence", "engagement",
    "conviction", "theme_fit", "catalyst",
]
PENALTY_COMPONENT = "risk_penalty"
SCORE_BASE = 12.0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _reconstruct_raw_score(components: dict) -> Optional[float]:
    """
    Reconstruct the raw (pre-clamp) score from the components dict.

    Formula (mirrors score_serenity_stock.py lines 138):
        raw = 12 + frequency + recency + persistence + engagement
              + conviction + theme_fit + catalyst - risk_penalty

    Returns None if the components dict does not have the expected keys
    (e.g. the no-evidence fallback returns different keys).
    """
    required = set(ADDITIVE_COMPONENTS + [PENALTY_COMPONENT])
    if not required.issubset(components.keys()):
        return None  # no-evidence symbol — cannot reconstruct
    raw = SCORE_BASE
    for k in ADDITIVE_COMPONENTS:
        raw += components[k]
    raw -= components[PENALTY_COMPONENT]
    return raw


def _variant_score(components: dict, remove: str) -> Optional[float]:
    """
    Leave-one-out: recompute clamped score with one component zeroed out.

    For additive components (frequency..catalyst): subtract its value.
    For risk_penalty: add it back (removing the penalty).
    Returns None when reconstruction is not possible (no-evidence symbols).
    """
    raw = _reconstruct_raw_score(components)
    if raw is None:
        return None
    if remove == PENALTY_COMPONENT:
        variant_raw = raw + components[PENALTY_COMPONENT]
    else:
        variant_raw = raw - components.get(remove, 0.0)
    return _clamp(variant_raw)


def evaluate_symbol_at_cutoff(
    db_path: Path,
    symbol: str,
    bars_before_cutoff: list[dict],
    cutoff_dt: datetime,
) -> Optional[dict]:
    """
    Compute quant score, indicators, and signal for *symbol* using only
    data available on or before *cutoff_dt*.

    Args:
        db_path:             Path to serenity.sqlite (for score_symbol).
        symbol:              Ticker string.
        bars_before_cutoff:  OHLCV bar dicts with date <= cutoff (oldest-first).
        cutoff_dt:           End-of-day cutoff datetime (timezone-aware UTC).

    Returns dict with keys:
        symbol, signal, score, components, entry_close, indicators
    or None if bars_before_cutoff is empty.
    """
    if not bars_before_cutoff:
        return None

    # Indicators — computed on truncated bar list only (zero look-ahead)
    indicators = _compute_all(bars_before_cutoff)

    # Quant score — now=cutoff_dt filters mentions to <= cutoff (zero look-ahead)
    try:
        score_result = _score_symbol(db_path, symbol, now=cutoff_dt)
    except Exception:
        score_result = {"score": None, "components": {}}

    score = score_result.get("score")
    components = score_result.get("components", {})

    # Signal — sentiment=None (honest: StockTwits only starts 2026-07-04,
    # after the 30-day-ago cutoff; all historical cutoffs also pre-date ST data)
    entry_close = bars_before_cutoff[-1]["close"]
    try:
        sig_result = _evaluate_signal(
            latest_close=entry_close,
            indicators=indicators,
            score=score,
            bars=bars_before_cutoff,
            prev_score=None,       # no archived prev_score in holdout context
            rr_ratio=2.0,
            sentiment=None,        # honest: no ST data pre-cutoff
        )
        signal = sig_result.get("signal", "NEUTRAL")
    except Exception:
        signal = "NEUTRAL"

    return {
        "symbol": symbol,
        "signal": signal,
        "score": score,
        "components": components,
        "entry_close": entry_close,
        "indicators": indicators,
    }


# ---------------------------------------------------------------------------
# Return computation
# ---------------------------------------------------------------------------

def _find_exit_price(
    all_bars: list[dict],
    cutoff_date_str: str,
) -> Optional[float]:
    """
    Return the last stored close price strictly after cutoff_date_str.
    This is the final available exit price (~holdout-days later).
    Returns None if no bars exist after cutoff.
    """
    after = [b["close"] for b in all_bars if b["date"] > cutoff_date_str]
    if not after:
        return None
    return after[-1]


def _holdout_return(entry: float, exit_: float) -> float:
    """Compute exit / entry - 1."""
    return exit_ / entry - 1.0


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.median(values)


def _win_rate(returns: list[float]) -> Optional[float]:
    if not returns:
        return None
    return sum(1 for r in returns if r > 0) / len(returns)


def _pct(val: Optional[float], decimals: int = 2) -> str:
    if val is None:
        return "    n/a"
    return f"{val * 100:+.{decimals}f}%"


def _fmt_rate(val: Optional[float]) -> str:
    if val is None:
        return "  n/a"
    return f"{val * 100:.0f}%"


# ---------------------------------------------------------------------------
# Quintile helpers
# ---------------------------------------------------------------------------

def _quintile_split(
    records: list[dict],
    score_key: str = "score",
) -> list[list[dict]]:
    """
    Sort records by score_key ascending and split into 5 equal quintiles.
    Records with None score are excluded.
    Returns a list of 5 sublists [Q1(lowest)..Q5(highest)].
    """
    valid = [r for r in records if r.get(score_key) is not None]
    valid.sort(key=lambda r: r[score_key])
    n = len(valid)
    if n < 5:
        return []
    size, rem = divmod(n, 5)
    quintiles = []
    start = 0
    for i in range(5):
        end = start + size + (1 if i < rem else 0)
        quintiles.append(valid[start:end])
        start = end
    return quintiles


def _quintile_stats(quintiles: list[list[dict]]) -> list[dict]:
    """Return per-quintile stats dict for display."""
    stats = []
    for i, q in enumerate(quintiles):
        rets = [r["holdout_return"] for r in q if r.get("holdout_return") is not None]
        scores = [r["score"] for r in q if r.get("score") is not None]
        stats.append({
            "quintile": f"Q{i+1}",
            "n": len(q),
            "score_range": f"{min(scores):.1f}–{max(scores):.1f}" if scores else "n/a",
            "median_return": _median(rets),
            "win_rate": _win_rate(rets),
            "n_returns": len(rets),
        })
    return stats


# ---------------------------------------------------------------------------
# Per-window evaluation engine
# ---------------------------------------------------------------------------

def run_window(
    db_path: Path,
    all_prices: dict[str, list[dict]],
    cutoff_date_str: str,
    label: str,
    min_bars: int = 60,
) -> dict:
    """
    Run the holdout evaluation for a single time window.

    Args:
        db_path:          Path to serenity.sqlite.
        all_prices:       Full price dict from _load_all_prices().
        cutoff_date_str:  Cutoff date as YYYY-MM-DD string.
        label:            Human-readable label for this window.
        min_bars:         Minimum bars before cutoff required (default 60).

    Returns:
        {
          "label": str,
          "cutoff": str,
          "records": [per-symbol dicts with signal/score/returns/components],
          "skipped_insufficient": [symbols],
          "skipped_no_exit": [symbols],
        }
    """
    cutoff_dt = datetime.fromisoformat(cutoff_date_str + "T23:59:59+00:00")
    records = []
    skipped_insufficient = []
    skipped_no_exit = []

    for symbol, bars in sorted(all_prices.items()):
        # Truncate to cutoff — zero look-ahead
        bars_pre = [b for b in bars if b["date"] <= cutoff_date_str]

        if len(bars_pre) < min_bars:
            skipped_insufficient.append(symbol)
            continue

        eval_result = evaluate_symbol_at_cutoff(
            db_path, symbol, bars_pre, cutoff_dt
        )
        if eval_result is None:
            skipped_insufficient.append(symbol)
            continue

        entry_close = eval_result["entry_close"]
        exit_close = _find_exit_price(bars, cutoff_date_str)

        if exit_close is None:
            skipped_no_exit.append(symbol)
            hret = None
        else:
            hret = _holdout_return(entry_close, exit_close)

        records.append({
            "symbol": symbol,
            "signal": eval_result["signal"],
            "score": eval_result["score"],
            "components": eval_result["components"],
            "entry": entry_close,
            "exit": exit_close,
            "holdout_return": hret,
        })

    return {
        "label": label,
        "cutoff": cutoff_date_str,
        "records": records,
        "skipped_insufficient": skipped_insufficient,
        "skipped_no_exit": skipped_no_exit,
    }


# ---------------------------------------------------------------------------
# Report: holdout signal-group breakdown (Task 1)
# ---------------------------------------------------------------------------

SIGNAL_ORDER = [
    "BUY_TRIGGER", "BUY_WATCH", "HOLD", "EXIT_ALERT", "OVERBOUGHT", "NEUTRAL",
]


def print_holdout_report(window: dict) -> None:
    """Print the per-signal-group holdout summary for the main window."""
    records = window["records"]
    cutoff = window["cutoff"]
    label = window["label"]

    # Compute universe benchmark (all records with a return)
    all_rets = [r["holdout_return"] for r in records if r["holdout_return"] is not None]
    universe_median = _median(all_rets)
    universe_winrate = _win_rate(all_rets)

    print()
    print("=" * 80)
    print(f"  TASK 1 — 30-Day Temporal Holdout   [{label}  |  cutoff: {cutoff}]")
    print("=" * 80)
    print(f"  Universe: {len(records)} symbols evaluated  |  "
          f"{len(window['skipped_insufficient'])} skipped (< 60 bars)  |  "
          f"{len(window['skipped_no_exit'])} skipped (no exit price)")
    print(f"  Benchmark median return (all evaluated symbols): "
          f"{_pct(universe_median)}  |  Win rate: {_fmt_rate(universe_winrate)}")
    print()

    hdr = (
        f"{'Signal':<14} | {'Count':>5} | {'Median Ret':>10} | "
        f"{'Win Rate':>8} | {'vs Universe':>11} | {'Best':>6} | {'Worst':>6}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    for sig in SIGNAL_ORDER:
        group = [r for r in records if r["signal"] == sig]
        rets = [r["holdout_return"] for r in group if r["holdout_return"] is not None]
        if not group:
            continue
        med = _median(rets)
        wr = _win_rate(rets)
        vs = (med - universe_median) if (med is not None and universe_median is not None) else None

        best_sym = worst_sym = "n/a"
        if rets:
            best_r = max(rets)
            worst_r = min(rets)
            best_sym = next(r["symbol"] for r in group if r["holdout_return"] == best_r)
            worst_sym = next(r["symbol"] for r in group if r["holdout_return"] == worst_r)

        print(
            f"{sig:<14} | {len(group):>5} | {_pct(med):>10} | "
            f"{_fmt_rate(wr):>8} | {_pct(vs):>11} | {best_sym:>6} | {worst_sym:>6}"
        )

    print(sep)
    print()

    # Honest verdict
    print("  HONEST VERDICT:")
    buy_groups = [r for r in records
                  if r["signal"] in ("BUY_TRIGGER", "BUY_WATCH")
                  and r["holdout_return"] is not None]
    buy_rets = [r["holdout_return"] for r in buy_groups]
    buy_med = _median(buy_rets)
    if buy_med is not None and universe_median is not None:
        if buy_med > universe_median and len(buy_rets) >= 5:
            print(f"  BUY signals (n={len(buy_rets)}) outperformed universe median by "
                  f"{_pct(buy_med - universe_median)} — "
                  "TENTATIVE EDGE, but sample size limits confidence.")
        elif buy_med > universe_median:
            print(f"  BUY signals (n={len(buy_rets)}) beat universe median by "
                  f"{_pct(buy_med - universe_median)}, but n<5 — INSUFFICIENT for conclusion.")
        else:
            print(f"  BUY signals (n={len(buy_rets)}) did NOT outperform universe median "
                  f"(delta={_pct(buy_med - universe_median)}). No demonstrated edge.")
    else:
        print("  Insufficient data for verdict.")

    print()

    # Per-symbol detail table
    print("  PER-SYMBOL DETAIL:")
    det_hdr = (
        f"  {'Symbol':<7} | {'Signal':<14} | {'Score':>6} | "
        f"{'Entry':>8} | {'Exit':>8} | {'Return':>8}"
    )
    det_sep = "-" * len(det_hdr)
    print(det_hdr)
    print(det_sep)
    for r in sorted(records, key=lambda x: (x["signal"], -(x["score"] or 0))):
        ret_str = _pct(r["holdout_return"]) if r["holdout_return"] is not None else "  no exit"
        exit_str = f"{r['exit']:.2f}" if r["exit"] is not None else "  n/a"
        score_str = f"{r['score']:.1f}" if r["score"] is not None else "  n/a"
        print(
            f"  {r['symbol']:<7} | {r['signal']:<14} | {score_str:>6} | "
            f"{r['entry']:>8.2f} | {exit_str:>8} | {ret_str:>8}"
        )
    print()


# ---------------------------------------------------------------------------
# Report: quintile table (Task 2)
# ---------------------------------------------------------------------------

def print_quintile_report(window: dict) -> None:
    """Print cross-sectional quintile analysis for one window."""
    records = window["records"]
    cutoff = window["cutoff"]
    label = window["label"]

    valid = [r for r in records if r.get("holdout_return") is not None
             and r.get("score") is not None]
    quintiles = _quintile_split(valid)

    print(f"  Window: {label}  (cutoff: {cutoff},  n={len(valid)} with returns)")
    if not quintiles:
        print("  [insufficient symbols for quintile split — skipped]")
        print()
        return

    stats = _quintile_stats(quintiles)
    q5_med = stats[4]["median_return"]
    q1_med = stats[0]["median_return"]
    spread = (q5_med - q1_med) if (q5_med is not None and q1_med is not None) else None

    hdr = (
        f"  {'Quintile':<10} | {'N':>4} | {'Score Range':>14} | "
        f"{'N Returns':>9} | {'Median Ret':>10} | {'Win Rate':>8}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)
    for s in stats:
        print(
            f"  {s['quintile']:<10} | {s['n']:>4} | {s['score_range']:>14} | "
            f"{s['n_returns']:>9} | {_pct(s['median_return']):>10} | "
            f"{_fmt_rate(s['win_rate']):>8}"
        )
    print(sep)
    mono = _check_monotonicity([s["median_return"] for s in stats])
    print(f"  Q5 - Q1 spread: {_pct(spread)}  |  Monotonicity: {mono}")
    print()


def _check_monotonicity(medians: list[Optional[float]]) -> str:
    """Classify quintile return sequence as monotone, partial, or none."""
    valid = [m for m in medians if m is not None]
    if len(valid) < 5:
        return "insufficient data"
    up = all(valid[i] <= valid[i + 1] for i in range(len(valid) - 1))
    down = all(valid[i] >= valid[i + 1] for i in range(len(valid) - 1))
    if up:
        return "MONOTONE INCREASING (score predicts return)"
    if down:
        return "MONOTONE DECREASING (score anti-predicts return)"
    # Count how many adjacent pairs are in the right direction
    ups = sum(1 for i in range(len(valid) - 1) if valid[i] < valid[i + 1])
    return f"NON-MONOTONE ({ups}/4 pairs ascending)"


# ---------------------------------------------------------------------------
# Report: leave-one-out weight scan (Task 3)
# ---------------------------------------------------------------------------

ALL_COMPONENTS = ADDITIVE_COMPONENTS + [PENALTY_COMPONENT]


def print_weight_scan_report(window: dict) -> None:
    """Print leave-one-out component weight scan for one window."""
    records = window["records"]
    cutoff = window["cutoff"]
    label = window["label"]

    print(f"  Window: {label}  (cutoff: {cutoff})")
    print()
    print("  NOTE: Variant scores are reconstructed from the components dict using")
    print(f"  formula: raw = {SCORE_BASE} + sum(additive_components) - risk_penalty,")
    print("  then clamped to [0,100]. For 'no-evidence' symbols (score_symbol returned")
    print("  the fallback 20 with non-standard component keys), those symbols are")
    print("  excluded from the variant quintile calculation (their score does not")
    print("  change across variants since components cannot be decomposed).")
    print()

    # Baseline quintile spread (original scores)
    base_records = [r for r in records if r.get("holdout_return") is not None
                    and r.get("score") is not None]
    base_quints = _quintile_split(base_records)
    if base_quints:
        base_stats = _quintile_stats(base_quints)
        base_spread = _quintile_spread(base_stats)
    else:
        base_spread = None

    hdr = (
        f"  {'Remove Component':<20} | {'Q5-Q1 Spread':>12} | "
        f"{'vs Baseline':>12} | {'Verdict':<40}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    # Baseline row
    base_mono = _check_monotonicity(
        [s["median_return"] for s in base_stats] if base_quints else []
    )
    print(
        f"  {'(baseline)':<20} | {_pct(base_spread):>12} | "
        f"{'—':>12} | {base_mono:<40}"
    )

    for comp in ALL_COMPONENTS:
        # Build variant records
        variant = []
        for r in records:
            if r.get("holdout_return") is None:
                continue
            v_score = _variant_score(r["components"], comp)
            if v_score is None:
                continue  # no-evidence symbol — skip
            variant.append({
                "symbol": r["symbol"],
                "score": v_score,
                "holdout_return": r["holdout_return"],
            })

        quints = _quintile_split(variant)
        if quints:
            stats = _quintile_stats(quints)
            spread = _quintile_spread(stats)
            mono = _check_monotonicity([s["median_return"] for s in stats])
        else:
            spread = None
            mono = "insufficient"

        delta = (
            (spread - base_spread)
            if (spread is not None and base_spread is not None)
            else None
        )

        # Determine verdict
        if delta is None:
            verdict = "cannot assess"
        elif delta > 0.005:   # > 0.5 pp improvement
            verdict = "IMPROVES monotonicity — consider removing"
        elif delta < -0.005:  # degrades
            verdict = "DEGRADES monotonicity — component adds value"
        else:
            verdict = "no material change"

        print(
            f"  {f'remove {comp}':<20} | {_pct(spread):>12} | "
            f"{_pct(delta):>12} | {verdict:<40}"
        )

    print(sep)
    print()

    # Specific hypothesis test
    print("  HYPOTHESIS: 'Removing recency improves predictive power'")
    recency_variant = []
    for r in records:
        if r.get("holdout_return") is None:
            continue
        v_score = _variant_score(r["components"], "recency")
        if v_score is None:
            continue
        recency_variant.append({
            "symbol": r["symbol"],
            "score": v_score,
            "holdout_return": r["holdout_return"],
        })
    rec_quints = _quintile_split(recency_variant)
    if rec_quints:
        rec_stats = _quintile_stats(rec_quints)
        rec_spread = _quintile_spread(rec_stats)
        rec_mono = _check_monotonicity([s["median_return"] for s in rec_stats])
        delta = (
            (rec_spread - base_spread)
            if (rec_spread is not None and base_spread is not None)
            else None
        )
        print(f"  Baseline Q5-Q1 spread : {_pct(base_spread)}")
        print(f"  No-recency Q5-Q1 spread: {_pct(rec_spread)}")
        print(f"  Delta                 : {_pct(delta)}")
        print(f"  Monotonicity (no-rec) : {rec_mono}")
        if delta is not None and delta > 0.005:
            print("  => HYPOTHESIS SUPPORTED: removing recency improves Q5-Q1 spread.")
        elif delta is not None and delta < -0.005:
            print("  => HYPOTHESIS REFUTED: removing recency degrades spread.")
        else:
            print("  => HYPOTHESIS INCONCLUSIVE: removing recency has no material effect.")
    else:
        print("  [insufficient data for recency hypothesis test]")
    print()


def _quintile_spread(stats: list[dict]) -> Optional[float]:
    """Return Q5 median − Q1 median, or None."""
    if len(stats) < 5:
        return None
    q5 = stats[4]["median_return"]
    q1 = stats[0]["median_return"]
    if q5 is None or q1 is None:
        return None
    return q5 - q1


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Serenity Signal — 30-day temporal holdout + quintile + weight scan"
    )
    ap.add_argument(
        "--db",
        default=None,
        help="Path to serenity.sqlite (default: data/serenity.sqlite in repo root)",
    )
    ap.add_argument(
        "--holdout-days",
        type=int,
        default=30,
        help="Number of calendar days to hold out (default: 30)",
    )
    args = ap.parse_args()

    db_path = _get_db_path(args.db)
    if not db_path.exists():
        raise SystemExit(f"[holdout] Database not found: {db_path}")

    print(f"[holdout] Database : {db_path}")

    con = sqlite3.connect(db_path)
    try:
        all_prices = _load_all_prices(con)
        max_date_str = _get_max_date(con)
    finally:
        con.close()

    max_date = datetime.strptime(max_date_str, "%Y-%m-%d").date()
    print(f"[holdout] Max price date : {max_date_str}")
    print(f"[holdout] Symbols with prices : {len(all_prices)}")

    # Define cutoff windows
    main_cutoff = (max_date - timedelta(days=args.holdout_days)).isoformat()
    cutoff_60 = (max_date - timedelta(days=60)).isoformat()
    cutoff_90 = (max_date - timedelta(days=90)).isoformat()

    windows_def = [
        (main_cutoff, f"Main holdout (T-{args.holdout_days}d)"),
        (cutoff_60,   "Historical window T-60d"),
        (cutoff_90,   "Historical window T-90d"),
    ]

    print(f"[holdout] Cutoff windows:")
    for cd, lbl in windows_def:
        print(f"  {lbl}: {cd}")

    # Run all three windows
    windows = []
    for cutoff_str, lbl in windows_def:
        print(f"\n[holdout] Evaluating window: {lbl} ...")
        win = run_window(db_path, all_prices, cutoff_str, lbl)
        windows.append(win)
        n_ok = sum(1 for r in win["records"] if r["holdout_return"] is not None)
        print(f"  → {len(win['records'])} symbols evaluated, "
              f"{n_ok} with exit prices, "
              f"{len(win['skipped_insufficient'])} skipped")

    main_window = windows[0]

    # -------------------------------------------------------------------
    # Task 1: Holdout signal-group report (main window only)
    # -------------------------------------------------------------------
    print_holdout_report(main_window)

    # -------------------------------------------------------------------
    # Task 2: Cross-sectional quintile test (all 3 windows)
    # -------------------------------------------------------------------
    print()
    print("=" * 80)
    print("  TASK 2 — Cross-Sectional Quintile Test (Score vs Forward Return)")
    print("  Same-window snapshot eliminates time confound (fixes D-1)")
    print("=" * 80)
    print()
    for win in windows:
        print_quintile_report(win)

    # -------------------------------------------------------------------
    # Task 3: Leave-one-out component weight scan (main window)
    # -------------------------------------------------------------------
    print()
    print("=" * 80)
    print("  TASK 3 — Component Weight Scan / Leave-One-Out (R-2, fixes D-3)")
    print("=" * 80)
    print()
    print_weight_scan_report(main_window)

    print("=" * 80)
    print("  END OF REPORT")
    print("=" * 80)


if __name__ == "__main__":
    main()
