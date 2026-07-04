#!/usr/bin/env python3
"""
Technical indicator computations for Serenity Signal.

Pure Python (stdlib only, no pandas/numpy).  Every function accepts a list of
bar dicts with keys: date, open, high, low, close, volume.  Bars must be
ordered oldest-first.  Functions return None (not a guessed number) when
there is insufficient history to compute the indicator reliably.

Exported helpers
----------------
compute_ema(closes, period)      -> list[float | None]
compute_rsi(closes, period)      -> list[float | None]
compute_macd(closes)             -> list[dict]
compute_bb(closes, period, std)  -> list[dict]
compute_atr(bars, period)        -> float | None
compute_volume_ratio(bars)       -> float | None
compute_all(bars)                -> dict   (the shape returned by /api/symbol)
"""

import math
from typing import Optional


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _safe_float(value) -> Optional[float]:
    """Return float(value) if finite and non-None, else None."""
    if value is None:
        return None
    try:
        f = float(value)
        if math.isfinite(f):
            return f
        return None
    except (TypeError, ValueError):
        return None


def _closes(bars) -> list:
    """Extract close prices, replacing invalid values with None."""
    return [_safe_float(b.get("close")) for b in bars]


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def compute_ema(closes: list, period: int) -> list:
    """
    Compute Exponential Moving Average over a list of close prices.

    Uses the standard multiplier k = 2/(period+1).  The first valid EMA is
    seeded from the simple average of the first `period` valid closes; prior
    positions return None.

    Returns a list of the same length as `closes`, with None where not yet
    computable.
    """
    result = [None] * len(closes)
    k = 2.0 / (period + 1)

    # Find the first run of `period` consecutive non-None values.
    seed_start = None
    seed_count = 0
    buf = []
    for i, c in enumerate(closes):
        if c is None:
            seed_count = 0
            buf = []
            continue
        buf.append(c)
        seed_count += 1
        if seed_count == period:
            seed_start = i
            break

    if seed_start is None:
        return result

    # Seed with SMA of the first `period` closes.
    ema = sum(buf) / period
    result[seed_start] = ema

    # Apply EMA multiplier to all subsequent closes.
    for i in range(seed_start + 1, len(closes)):
        c = closes[i]
        if c is None:
            # Gap in data: propagate the last ema unchanged in result, leave
            # current slot None to signal broken continuity.
            result[i] = None
        else:
            ema = c * k + ema * (1 - k)
            result[i] = ema

    return result


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def compute_rsi(closes: list, period: int = 14) -> list:
    """
    Compute RSI using Wilder's smoothed average (standard method).

    Returns a list with None where insufficient data, otherwise floats in
    [0, 100].
    """
    result = [None] * len(closes)

    # Build a list of changes ignoring None gaps.
    # We need `period` gains/losses for the initial average, so we
    # process index-by-index.
    last_close = None
    gains = []
    losses = []
    avg_gain = avg_loss = None
    ema_start = None

    for i, c in enumerate(closes):
        if c is None:
            last_close = None
            continue
        if last_close is None:
            last_close = c
            continue

        change = c - last_close
        last_close = c
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0

        if avg_gain is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) == period:
                avg_gain = sum(gains) / period
                avg_loss = sum(losses) / period
                ema_start = i
                rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
                result[i] = 100.0 - (100.0 / (1.0 + rs))
        else:
            # Wilder smoothing
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
            result[i] = 100.0 - (100.0 / (1.0 + rs))

    return result


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def compute_macd(
    closes: list,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list:
    """
    Compute MACD line, signal line, and histogram.

    Returns a list of dicts:
        {"macd": float|None, "signal": float|None, "histogram": float|None}
    """
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)

    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)

    signal_line = compute_ema(macd_line, signal)

    result = []
    for m, sig in zip(macd_line, signal_line):
        if m is None or sig is None:
            result.append({"macd": m, "signal": sig, "histogram": None})
        else:
            result.append({"macd": m, "signal": sig, "histogram": m - sig})
    return result


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def compute_bb(closes: list, period: int = 20, num_std: float = 2.0) -> list:
    """
    Compute Bollinger Bands (upper, mid, lower).

    The middle band is an SMA.  Standard deviation uses the population formula
    (same as most charting platforms).

    Returns a list of dicts:
        {"upper": float|None, "mid": float|None, "lower": float|None}
    """
    result = [{"upper": None, "mid": None, "lower": None} for _ in closes]
    window = []

    for i, c in enumerate(closes):
        if c is None:
            window = []  # reset on gap
            continue
        window.append(c)
        if len(window) < period:
            continue
        if len(window) > period:
            window.pop(0)
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = math.sqrt(variance)
        result[i] = {
            "upper": sma + num_std * std,
            "mid": sma,
            "lower": sma - num_std * std,
        }
    return result


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def compute_atr(bars: list, period: int = 14) -> Optional[float]:
    """
    Compute the most-recent ATR(period) using Wilder's smoothing.

    Requires bars with open/high/low/close.  Returns a single float (the
    latest ATR) or None if insufficient data.
    """
    trs = []
    prev_close = None
    for b in bars:
        h = _safe_float(b.get("high"))
        l = _safe_float(b.get("low"))
        c = _safe_float(b.get("close"))
        if h is None or l is None or c is None:
            prev_close = None
            continue
        if prev_close is None:
            # First bar: TR = high - low (no previous close available)
            trs.append(h - l)
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)
        prev_close = c

    if len(trs) < period:
        return None

    # Seed ATR from simple average of first `period` TRs.
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---------------------------------------------------------------------------
# Volume Ratio
# ---------------------------------------------------------------------------

def compute_volume_ratio(bars: list, avg_period: int = 20) -> Optional[float]:
    """
    Return latest_volume / 20-day average volume.

    Returns None if fewer than avg_period bars have valid volume.
    """
    vols = []
    for b in reversed(bars):
        v = _safe_float(b.get("volume"))
        if v is not None and v >= 0:
            vols.append(v)
        if len(vols) > avg_period:
            break

    if len(vols) < avg_period:
        return None

    latest = vols[0]
    avg = sum(vols[1:avg_period]) / (avg_period - 1)
    if avg == 0:
        return None
    return latest / avg


# ---------------------------------------------------------------------------
# Aggregate: compute_all
# ---------------------------------------------------------------------------

def compute_all(bars: list) -> dict:
    """
    Compute all indicators from a list of OHLCV bar dicts.

    Returns a dict with:
        ema20        list[float|None]   – per-bar EMA(20)
        ema50        list[float|None]   – per-bar EMA(50)
        rsi14        list[float|None]   – per-bar RSI(14)
        macd         list[dict]         – per-bar {macd, signal, histogram}
        bb           list[dict]         – per-bar {upper, mid, lower}
        atr14        float|None         – latest ATR(14)
        volume_ratio float|None         – latest vol / 20-day avg vol
    """
    closes = _closes(bars)
    return {
        "ema20": compute_ema(closes, 20),
        "ema50": compute_ema(closes, 50),
        "rsi14": compute_rsi(closes, 14),
        "macd": compute_macd(closes),
        "bb": compute_bb(closes, 20),
        "atr14": compute_atr(bars, 14),
        "volume_ratio": compute_volume_ratio(bars, 20),
    }
