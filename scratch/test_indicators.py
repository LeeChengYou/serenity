#!/usr/bin/env python3
"""
Test suite for scripts/indicators.py.

Sections
--------
1. Unit tests with hand-computed reference values (small deterministic series).
2. Real-data sanity checks against NVDA, MU, AAOI from the local SQLite DB.

Run with:
    python scratch/test_indicators.py
"""

import io
import math
import sqlite3
import sys
from pathlib import Path

# Force UTF-8 output so the test file works on Windows consoles with cp950/cp932.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from indicators import (
    compute_ema,
    compute_rsi,
    compute_macd,
    compute_bb,
    compute_atr,
    compute_volume_ratio,
    compute_all,
)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def assert_close(label, actual, expected, tol=1e-4):
    if actual is None and expected is None:
        print(f"  {PASS}  {label}: None == None")
        return True
    if actual is None or expected is None:
        print(f"  {FAIL}  {label}: got {actual!r}, expected {expected!r}")
        return False
    if abs(actual - expected) <= tol:
        print(f"  {PASS}  {label}: {actual:.6f} ≈ {expected:.6f}")
        return True
    print(f"  {FAIL}  {label}: {actual:.6f} ≠ {expected:.6f} (diff={abs(actual-expected):.6f})")
    return False


def assert_true(label, condition, extra=""):
    if condition:
        print(f"  {PASS}  {label} {extra}")
        return True
    print(f"  {FAIL}  {label} {extra}")
    return False


# ---------------------------------------------------------------------------
# 1. Unit tests — hand-computed reference values
# ---------------------------------------------------------------------------

def test_ema_small():
    """EMA(3) on [1,2,3,4,5] with k=0.5.

    seed (SMA of first 3) = (1+2+3)/3 = 2.0
    bar3 = 4 * 0.5 + 2.0 * 0.5 = 3.0
    bar4 = 5 * 0.5 + 3.0 * 0.5 = 4.0
    """
    print("\n=== EMA unit test ===")
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = compute_ema(closes, 3)
    assert_true("result length matches", len(result) == 5)
    assert_true("first two slots are None", result[0] is None and result[1] is None)
    assert_close("seed EMA (idx=2)", result[2], 2.0)
    assert_close("EMA at idx=3",     result[3], 3.0)
    assert_close("EMA at idx=4",     result[4], 4.0)


def test_ema_insufficient():
    print("\n=== EMA insufficient data ===")
    result = compute_ema([1.0, 2.0], 5)
    assert_true("all None when period > len", all(r is None for r in result))


def test_rsi_small():
    """RSI(3) on a 5-bar series.

    Changes:  [+1, +1, +1, -1]
    After 3 changes (period=3):
      avg_gain = (1+1+1)/3 = 1.0
      avg_loss = 0/3 = 0.0
      RS = inf → RSI = 100.0

    Then change = -1:
      avg_gain = (1*2 + 0)/3 = 2/3
      avg_loss = (0*2 + 1)/3 = 1/3
      RS = (2/3)/(1/3) = 2 → RSI = 100 - 100/(1+2) = 66.666...
    """
    print("\n=== RSI unit test ===")
    closes = [10.0, 11.0, 12.0, 13.0, 12.0]
    result = compute_rsi(closes, period=3)
    assert_true("length == 5", len(result) == 5)
    # idx 0 and 1 are None (need 3 changes = 4 prices minimum for first RSI)
    assert_true("idx 0 is None", result[0] is None)
    assert_true("idx 1 is None", result[1] is None)
    assert_true("idx 2 is None", result[2] is None)
    # First RSI appears at idx 3 (after 3 up-changes)
    assert_close("RSI at idx=3 (all gains)", result[3], 100.0)
    assert_close("RSI at idx=4 (one loss)",  result[4], 66.6667, tol=0.001)


def test_bb_small():
    """BB(3, 2σ) on [10, 20, 30].

    SMA = 20, variance = ((10-20)^2+(20-20)^2+(30-20)^2)/3 = 200/3
    std = sqrt(200/3) ≈ 8.16497
    upper = 20 + 2*8.16497 ≈ 36.3299
    lower = 20 - 2*8.16497 ≈ 3.6701
    """
    print("\n=== BB unit test ===")
    closes = [10.0, 20.0, 30.0]
    result = compute_bb(closes, period=3, num_std=2.0)
    assert_true("length == 3", len(result) == 3)
    assert_true("idx 0 mid is None", result[0]["mid"] is None)
    assert_true("idx 1 mid is None", result[1]["mid"] is None)
    assert_close("BB mid at idx=2",   result[2]["mid"],   20.0)
    assert_close("BB upper at idx=2", result[2]["upper"], 20 + 2 * math.sqrt(200/3), tol=1e-4)
    assert_close("BB lower at idx=2", result[2]["lower"], 20 - 2 * math.sqrt(200/3), tol=1e-4)


def test_atr_small():
    """ATR(2) on 3 bars.

    bar0: H=12, L=10, C=11   — first bar, TR = H-L = 2
    bar1: H=13, L=11, C=12   — TR = max(13-11, |13-11|, |11-11|) = 2
    bar2: H=15, L=12, C=14   — TR = max(15-12, |15-12|, |12-12|) = 3

    Seed ATR (period=2) = avg(TR0,TR1) = (2+2)/2 = 2.0
    Wilder step for TR2=3: atr = (2.0*1 + 3) / 2 = 2.5
    """
    print("\n=== ATR unit test ===")
    bars = [
        {"high": 12, "low": 10, "close": 11},
        {"high": 13, "low": 11, "close": 12},
        {"high": 15, "low": 12, "close": 14},
    ]
    atr = compute_atr(bars, period=2)
    assert_close("ATR(2) on 3 bars", atr, 2.5)


def test_atr_insufficient():
    print("\n=== ATR insufficient data ===")
    bars = [{"high": 5, "low": 4, "close": 4.5}]
    assert_true("ATR None when 1 bar and period=14", compute_atr(bars, 14) is None)


def test_volume_ratio():
    """volume_ratio with avg_period=3.

    bars (reversed): [10, 5, 5, 20, ...]
    latest = 10, avg of next 2 = (5+5)/2 = 5, ratio = 2.0
    """
    print("\n=== volume_ratio unit test ===")
    bars = [{"volume": 20}, {"volume": 5}, {"volume": 5}, {"volume": 10}]
    ratio = compute_volume_ratio(bars, avg_period=3)
    assert_close("volume_ratio", ratio, 2.0)


# ---------------------------------------------------------------------------
# 2. Real-data sanity checks
# ---------------------------------------------------------------------------

def load_bars(symbol, db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "select date, open, high, low, close, volume from prices where symbol=? order by date",
        (symbol,),
    ).fetchall()
    con.close()
    return [
        {
            "date": r[0], "open": r[1], "high": r[2],
            "low": r[3], "close": r[4], "volume": r[5],
        }
        for r in rows
    ]


def real_data_sanity(symbol, bars):
    print(f"\n=== Real-data sanity: {symbol} ({len(bars)} bars) ===")
    if not bars:
        print(f"  SKIP — no bars for {symbol}")
        return

    indic = compute_all(bars)

    # RSI
    rsi_vals = [v for v in indic["rsi14"] if v is not None]
    if rsi_vals:
        last_rsi = rsi_vals[-1]
        print(f"  RSI(14) last value : {last_rsi:.2f}")
        assert_true("RSI in [0, 100]", 0 <= last_rsi <= 100, f"(got {last_rsi:.2f})")
    else:
        print("  RSI(14): insufficient data (all None)")

    # EMA20
    ema20_vals = [v for v in indic["ema20"] if v is not None]
    if ema20_vals:
        last_ema20 = ema20_vals[-1]
        closes = [b["close"] for b in bars if b["close"] is not None]
        min_close = min(closes[-40:]) if len(closes) >= 40 else min(closes)
        max_close = max(closes[-40:]) if len(closes) >= 40 else max(closes)
        print(f"  EMA(20) last value : {last_ema20:.4f}  "
              f"(40-bar close range: {min_close:.4f}–{max_close:.4f})")
        assert_true(
            "EMA20 within 40-bar price range",
            min_close * 0.8 <= last_ema20 <= max_close * 1.2,
            f"(EMA20={last_ema20:.4f})",
        )

    # EMA50
    ema50_vals = [v for v in indic["ema50"] if v is not None]
    if ema50_vals:
        last_ema50 = ema50_vals[-1]
        print(f"  EMA(50) last value : {last_ema50:.4f}")

    # MACD
    macd_vals = [v for v in indic["macd"] if v["macd"] is not None]
    if macd_vals:
        last_macd = macd_vals[-1]
        print(f"  MACD last: macd={last_macd['macd']:.4f}, "
              f"signal={last_macd['signal']:.4f}, "
              f"histogram={last_macd['histogram']:.4f}")

    # Bollinger Bands
    bb_vals = [v for v in indic["bb"] if v["mid"] is not None]
    if bb_vals:
        last_bb = bb_vals[-1]
        print(f"  BB last: lower={last_bb['lower']:.4f}, "
              f"mid={last_bb['mid']:.4f}, upper={last_bb['upper']:.4f}")
        assert_true(
            "BB lower ≤ mid ≤ upper",
            last_bb["lower"] <= last_bb["mid"] <= last_bb["upper"],
            f"(l={last_bb['lower']:.4f} m={last_bb['mid']:.4f} u={last_bb['upper']:.4f})",
        )

    # ATR
    atr = indic["atr14"]
    if atr is not None:
        print(f"  ATR(14) : {atr:.4f}")
        assert_true("ATR > 0", atr > 0, f"(got {atr:.4f})")
    else:
        print("  ATR(14): insufficient data (None)")

    # Volume ratio
    vr = indic["volume_ratio"]
    if vr is not None:
        print(f"  Volume ratio (latest / 20-day avg): {vr:.4f}")
        assert_true("Volume ratio > 0", vr > 0)
    else:
        print("  Volume ratio: insufficient data (None)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # --- Unit tests ---
    test_ema_small()
    test_ema_insufficient()
    test_rsi_small()
    test_bb_small()
    test_atr_small()
    test_atr_insufficient()
    test_volume_ratio()

    # --- Real-data sanity ---
    db_path = ROOT / "data" / "serenity.sqlite"
    if not db_path.exists():
        print(f"\n[SKIP] Real-data tests: DB not found at {db_path}")
    else:
        for sym in ["NVDA", "MU", "AAOI"]:
            bars = load_bars(sym, str(db_path))
            real_data_sanity(sym, bars)

    print("\nAll tests completed.")
