#!/usr/bin/env python3
"""
Backtest framework for Serenity Signal — SPEC F-11.

For every (symbol, mention_date) pair that has >= 20 trading days of
REAL subsequent price history in the database, this script computes
forward returns at T+5, T+10, T+20, and T+60 calendar-adjacent trading
days, then buckets results by Serenity scorecard score range.

AUTHENTICITY GUARANTEE
- All prices come from the real serenity.sqlite database.
- Returns are computed as: price(T+N) / price(T) - 1
- No prices, returns, or signals are ever fabricated or hardcoded.
- If T+N price is unavailable the sample is excluded for that horizon.

Usage:
    python scripts/backtest_signals.py [--db PATH]
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Quantitative X-corpus scorer (point-in-time capable) — imported from the
# serenity-stock-scorer skill so we share a single scoring implementation.
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


_score_symbol = _load_quant_scorer()


def quant_score_asof(db_path: Path, symbol: str, mention_date: str) -> float | None:
    """
    Compute the quantitative X-corpus score for `symbol` using only mentions
    dated on or before `mention_date` (YYYY-MM-DD).  Returns None on failure.

    The as-of time is the end of the mention day so same-day mentions are
    included while future mentions are excluded (no look-ahead bias).
    """
    try:
        as_of = datetime.fromisoformat(mention_date + "T23:59:59+00:00")
    except ValueError:
        return None
    try:
        result = _score_symbol(db_path, symbol, now=as_of)
        return result.get("score")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    here = Path(__file__).resolve().parent
    return here.parent / "data" / "serenity.sqlite"


def _load_prices(con: sqlite3.Connection) -> dict[str, list[tuple[str, float]]]:
    """
    Return {symbol: [(date, close), ...]} oldest-first, real data only.
    Rows with NULL or non-positive close are excluded.
    """
    rows = con.execute(
        "select symbol, date, close from prices "
        "where close is not null and close > 0 "
        "order by symbol, date"
    ).fetchall()

    prices: dict[str, list[tuple[str, float]]] = {}
    for sym, date, close in rows:
        prices.setdefault(sym, []).append((date, float(close)))
    return prices


def _load_mentions(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """
    Return [(symbol, mention_date), ...] de-duplicated to one row per
    (symbol, trading-day).  `mention_date` is normalised to YYYY-MM-DD.
    """
    rows = con.execute(
        "select distinct symbol, substr(mentioned_at, 1, 10) as day "
        "from mentions "
        "order by symbol, day"
    ).fetchall()
    return [(sym, day) for sym, day in rows]


def _load_scores(con: sqlite3.Connection) -> dict[str, float]:
    """Return {symbol: final_score} from the latest scorecards."""
    rows = con.execute("select symbol, final_score from scorecards").fetchall()
    return {sym: score for sym, score in rows if score is not None}


# ---------------------------------------------------------------------------
# Forward return computation
# ---------------------------------------------------------------------------

HORIZONS = [5, 10, 20, 60]  # trading days forward


def _find_price_at_or_after(
    price_list: list[tuple[str, float]],
    start_idx: int,
    trading_days_forward: int,
) -> float | None:
    """
    Return the close price exactly `trading_days_forward` bars after
    `start_idx`, or None if the history is too short.

    Using bar-index offsets (not calendar days) ensures we only count
    real trading sessions stored in the database.
    """
    target_idx = start_idx + trading_days_forward
    if target_idx >= len(price_list):
        return None
    return price_list[target_idx][1]


def compute_forward_returns(
    price_list: list[tuple[str, float]],
    mention_date: str,
) -> dict[str, float | None] | None:
    """
    For a given mention date, find the nearest price bar on or after that
    date, then compute forward returns at each horizon.

    Returns None if the mention date predates all stored prices, or if
    there are fewer than 20 subsequent bars (SPEC requirement).
    """
    # Find the index of the bar on or after mention_date
    entry_idx = None
    for i, (date, _) in enumerate(price_list):
        if date >= mention_date:
            entry_idx = i
            break

    if entry_idx is None:
        return None

    entry_price = price_list[entry_idx][1]
    if entry_price <= 0:
        return None

    # Require at least 20 subsequent bars
    bars_after = len(price_list) - entry_idx - 1
    if bars_after < 20:
        return None

    results: dict[str, float | None] = {}
    for h in HORIZONS:
        fwd_price = _find_price_at_or_after(price_list, entry_idx, h)
        if fwd_price is not None and fwd_price > 0:
            results[f"t{h}"] = fwd_price / entry_price - 1.0
        else:
            results[f"t{h}"] = None

    return results


# ---------------------------------------------------------------------------
# Bucketing and statistics
# ---------------------------------------------------------------------------

SCORE_BUCKETS = [
    (80, 100, "80-100"),
    (60, 80,  "60-80"),
    (40, 60,  "40-60"),
    (0,  40,  "0-40"),
]


def _bucket_for(score: float | None) -> str | None:
    if score is None:
        return None
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= score <= hi:
            return label
    return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def _win_rate(returns: list[float]) -> float | None:
    if not returns:
        return None
    wins = sum(1 for r in returns if r > 0)
    return wins / len(returns)


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(db_path: Path) -> dict:
    """
    Run the full backtest and return structured results.

    Returns:
        {
          "buckets": {
            "80-100": {"t5": {...}, "t10": {...}, "t20": {...}, "t60": {...}},
            ...
          },
          "total_samples": int,
          "symbols_covered": [str, ...],
          "skipped_no_prices": int,
          "skipped_short_history": int,
        }
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    con = sqlite3.connect(db_path)
    try:
        prices = _load_prices(con)
        mentions = _load_mentions(con)
    finally:
        con.close()

    # Accumulate returns per bucket per horizon
    bucket_returns: dict[str, dict[str, list[float]]] = {
        label: {f"t{h}": [] for h in HORIZONS}
        for _, _, label in SCORE_BUCKETS
    }
    bucket_returns["unknown"] = {f"t{h}": [] for h in HORIZONS}

    total_samples = 0
    skipped_no_prices = 0
    skipped_short_history = 0
    symbols_seen: set[str] = set()

    for sym, mention_date in mentions:
        if sym not in prices:
            skipped_no_prices += 1
            continue

        fwd = compute_forward_returns(prices[sym], mention_date)
        if fwd is None:
            skipped_short_history += 1
            continue

        total_samples += 1
        symbols_seen.add(sym)

        # Point-in-time quantitative score: uses only mentions up to the
        # mention day, so the bucket reflects what was knowable at entry.
        score = quant_score_asof(db_path, sym, mention_date)
        bucket = _bucket_for(score) or "unknown"

        for h in HORIZONS:
            key = f"t{h}"
            val = fwd.get(key)
            if val is not None:
                bucket_returns[bucket][key].append(val)

    # Aggregate statistics per bucket per horizon
    results: dict[str, dict] = {}
    for _, _, label in SCORE_BUCKETS:
        results[label] = {}
        for h in HORIZONS:
            key = f"t{h}"
            vals = bucket_returns[label][key]
            results[label][key] = {
                "median_return": _median(vals),
                "win_rate": _win_rate(vals),
                "samples": len(vals),
            }

    # unknown bucket
    if any(bucket_returns["unknown"][f"t{h}"] for h in HORIZONS):
        results["unknown"] = {}
        for h in HORIZONS:
            key = f"t{h}"
            vals = bucket_returns["unknown"][key]
            results["unknown"][key] = {
                "median_return": _median(vals),
                "win_rate": _win_rate(vals),
                "samples": len(vals),
            }

    return {
        "buckets": results,
        "total_samples": total_samples,
        "symbols_covered": sorted(symbols_seen),
        "skipped_no_prices": skipped_no_prices,
        "skipped_short_history": skipped_short_history,
    }


# ---------------------------------------------------------------------------
# CLI output formatter
# ---------------------------------------------------------------------------

def _pct(val: float | None, decimals: int = 1) -> str:
    if val is None:
        return "  n/a  "
    return f"{val * 100:+.{decimals}f}%"


def _rate(val: float | None) -> str:
    if val is None:
        return " n/a "
    return f"{val * 100:.0f}%"


def print_table(data: dict) -> None:
    buckets = data["buckets"]
    bucket_order = ["80-100", "60-80", "40-60", "0-40"]
    if "unknown" in buckets:
        bucket_order.append("unknown")

    header = (
        f"{'Score Bucket':<14} | "
        f"{'Median T+5':>11} | "
        f"{'Median T+10':>12} | "
        f"{'Median T+20':>12} | "
        f"{'Median T+60':>12} | "
        f"{'Win% T+20':>10} | "
        f"{'Samples T+20':>13}"
    )
    sep = "-" * len(header)
    print()
    print("=" * len(header))
    print("  Serenity Signal — Forward Return Backtest (SPEC F-11)")
    print("=" * len(header))
    print(header)
    print(sep)

    for label in bucket_order:
        if label not in buckets:
            continue
        b = buckets[label]
        t5  = b.get("t5",  {})
        t10 = b.get("t10", {})
        t20 = b.get("t20", {})
        t60 = b.get("t60", {})
        row = (
            f"{label:<14} | "
            f"{_pct(t5.get('median_return')):>11} | "
            f"{_pct(t10.get('median_return')):>12} | "
            f"{_pct(t20.get('median_return')):>12} | "
            f"{_pct(t60.get('median_return')):>12} | "
            f"{_rate(t20.get('win_rate')):>10} | "
            f"{t20.get('samples', 0):>13}"
        )
        print(row)

    print(sep)
    print(f"Total (symbol, mention_date) samples included : {data['total_samples']}")
    print(f"Symbols covered                               : {', '.join(data['symbols_covered'])}")
    print(f"Skipped — no price history                    : {data['skipped_no_prices']}")
    print(f"Skipped — fewer than 20 subsequent bars       : {data['skipped_short_history']}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Serenity Signal backtest — SPEC F-11"
    )
    ap.add_argument(
        "--db",
        default=None,
        help="Path to serenity.sqlite (default: data/serenity.sqlite relative to repo root)",
    )
    args = ap.parse_args()

    db_path = _get_db_path(args.db)
    print(f"[backtest] Using database: {db_path}")

    data = run_backtest(db_path)
    print_table(data)


if __name__ == "__main__":
    main()
