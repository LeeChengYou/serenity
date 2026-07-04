#!/usr/bin/env python3
"""
Regression test for D-2: evaluate_signal() must return rsi (and other key
numerics) as explicit top-level fields so consumers never parse condition text.

Run with:
    python scratch/test_signal_rsi_field.py
"""

import io
import math
import sys
from pathlib import Path

# Force UTF-8 output so the test works on Windows consoles with cp950/cp932.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from signals import evaluate_signal
from indicators import compute_all

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_failures = []


def ok(label, condition, extra=""):
    if condition:
        print(f"  {PASS}  {label} {extra}")
    else:
        print(f"  {FAIL}  {label} {extra}")
        _failures.append(label)
    return condition


# ---------------------------------------------------------------------------
# Synthetic bar data helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int, base_close: float = 100.0, drift: float = 0.0) -> list:
    """
    Generate n synthetic OHLCV bars (oldest-first) with a small upward drift.

    Parameters are chosen to produce realistic-looking data:
    - close oscillates around base_close with slight drift
    - high/low bracket each close by ±1%
    - volume is constant (sufficient for volume_ratio computation)
    """
    bars = []
    close = base_close
    for i in range(n):
        close = close * (1 + drift) + (1.0 if i % 3 == 0 else -0.5)
        high  = close * 1.01
        low   = close * 0.99
        bars.append({
            "date":   f"2026-01-{(i % 28) + 1:02d}",
            "open":   round(close - 0.25, 4),
            "high":   round(high, 4),
            "low":    round(low, 4),
            "close":  round(close, 4),
            "volume": 1_000_000,
        })
    return bars


def _rsi_from_conditions(result: dict):
    """
    Parse RSI value from the condition text (old fragile path).
    Returns None if no RSI condition detail is found.
    """
    for cond in result.get("conditions", []):
        detail = cond.get("detail", "")
        if "RSI: " in detail:
            try:
                return float(detail.split("RSI: ")[1].split()[0])
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Test: sufficient data (ema50 + rsi + atr all computable)
# ---------------------------------------------------------------------------

def test_rsi_field_present_when_data_sufficient():
    print("\n=== D-2: rsi field present when data is sufficient ===")

    # Need ≥50 bars for EMA50, ≥15 bars for RSI(14), ≥15 bars for ATR(14)
    bars = _make_bars(60, base_close=150.0, drift=0.001)
    indicators = compute_all(bars)
    latest_close = bars[-1]["close"]

    result = evaluate_signal(
        latest_close=latest_close,
        indicators=indicators,
        score=72.0,
        bars=bars,
    )

    # Core D-2 assertions
    ok("'rsi' key exists in result", "rsi" in result)
    ok("'rsi' is not None (sufficient bars)", result.get("rsi") is not None)
    ok("'rsi' is a float", isinstance(result.get("rsi"), float))
    ok("'rsi' in valid range [0, 100]", 0.0 <= result.get("rsi", -1) <= 100.0)

    # Verify structured field matches the value referenced in condition text
    rsi_from_conds = _rsi_from_conditions(result)
    if rsi_from_conds is not None and result.get("rsi") is not None:
        # Condition text uses 1 d.p.; structured field uses 4 d.p. — compare at 1 d.p.
        ok(
            "rsi field matches value in condition text (to 1 d.p.)",
            round(result["rsi"], 1) == round(rsi_from_conds, 1),
            f"(structured={result['rsi']:.4f}, from_text={rsi_from_conds:.1f})",
        )
    else:
        ok("rsi value found in condition text for cross-check", rsi_from_conds is not None)

    # Other new numeric fields
    ok("'ema50' key exists", "ema50" in result)
    ok("'ema50' is not None", result.get("ema50") is not None)
    ok("'volume_ratio' key exists", "volume_ratio" in result)

    # Existing fields must not have disappeared (additive-only contract)
    for field in ("signal", "conditions", "atr14", "ema20_ref", "score",
                  "entry_zone", "stop_loss", "risk_per_share", "target",
                  "rr_ratio", "insufficient_data"):
        ok(f"pre-existing field '{field}' still present", field in result)

    print(f"  [info] signal={result['signal']}, rsi={result.get('rsi')}, "
          f"ema50={result.get('ema50')}, atr14={result.get('atr14')}")


# ---------------------------------------------------------------------------
# Test: insufficient data (too few bars → rsi None, no crash)
# ---------------------------------------------------------------------------

def test_rsi_field_none_when_insufficient():
    print("\n=== D-2: rsi field is None when data is insufficient ===")

    # Only 5 bars — RSI(14) and EMA50 cannot be computed
    bars = _make_bars(5, base_close=100.0)
    indicators = compute_all(bars)
    latest_close = bars[-1]["close"]

    result = evaluate_signal(
        latest_close=latest_close,
        indicators=indicators,
        score=None,
        bars=bars,
    )

    ok("'rsi' key exists even with insufficient data", "rsi" in result)
    ok("'rsi' is None when bars < 15", result.get("rsi") is None)
    ok("insufficient_data=True", result.get("insufficient_data") is True)

    print(f"  [info] signal={result['signal']}, rsi={result.get('rsi')}, "
          f"insufficient_data={result.get('insufficient_data')}")


# ---------------------------------------------------------------------------
# Test: snapshot writer no longer needs condition-text parsing
# ---------------------------------------------------------------------------

def test_snapshot_writer_uses_structured_rsi():
    """
    Simulate the snapshot_signals() RSI extraction logic from server.py.
    Primary path (sp.get('rsi')) must succeed without touching condition text.
    """
    print("\n=== D-2: snapshot writer primary path (structured field) ===")

    bars = _make_bars(60, base_close=200.0, drift=0.002)
    indicators = compute_all(bars)
    latest_close = bars[-1]["close"]

    sp = evaluate_signal(
        latest_close=latest_close,
        indicators=indicators,
        score=68.0,
        bars=bars,
    )

    # Replicate exactly what the new snapshot_signals() does (primary path)
    rsi_val = sp.get("rsi")

    ok("rsi_val is not None via primary path", rsi_val is not None)
    ok("rsi_val is float", isinstance(rsi_val, float))
    ok("rsi_val in [0, 100]", 0.0 <= rsi_val <= 100.0)

    # Confirm the fallback path (condition-text parse) would also agree
    rsi_from_text = _rsi_from_conditions(sp)
    if rsi_from_text is not None and rsi_val is not None:
        ok(
            "primary path agrees with fallback parse (to 1 d.p.)",
            round(rsi_val, 1) == round(rsi_from_text, 1),
            f"(structured={rsi_val:.4f}, text={rsi_from_text:.1f})",
        )

    print(f"  [info] rsi_val={rsi_val}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_rsi_field_present_when_data_sufficient()
    test_rsi_field_none_when_insufficient()
    test_snapshot_writer_uses_structured_rsi()

    print()
    if _failures:
        print(f"\033[91m{len(_failures)} test(s) FAILED: {_failures}\033[0m")
        sys.exit(1)
    else:
        print("\033[92mAll D-2 regression tests PASSED.\033[0m")
