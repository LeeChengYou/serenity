#!/usr/bin/env python3
"""
Signal rules engine for Serenity Signal — SPEC F-06 / F-07.

Pure, unit-testable functions with NO web-server or database imports.
All inputs must be pre-computed by the caller from real stored data.

Signal states (in priority order, highest first):
    OVERBOUGHT   — RSI > 75 AND 20-day gain > 30 %
    EXIT_ALERT   — price < EMA50 OR RSI < 40 OR score_drop > 15
    BUY_TRIGGER  — BUY_WATCH conditions + volume_ratio > 1.5 + MACD histogram > 0
    BUY_WATCH    — score >= 70 AND price > EMA20 AND RSI < 65
    HOLD         — price > EMA50 AND RSI in [50, 70] AND score >= 55
    NEUTRAL      — default (insufficient conditions met or data missing)

Public API
----------
evaluate_signal(latest_close, indicators, score, bars,
                prev_score=None, rr_ratio=2.0) -> dict
position_sizing(entry, atr14, rr_ratio=2.0) -> dict
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _latest(series: list) -> Optional[float]:
    """Return the last non-None value in a per-bar list, or None."""
    if not series:
        return None
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _prev(series: list) -> Optional[float]:
    """Return the second-to-last non-None value in a per-bar list, or None."""
    if not series:
        return None
    found = 0
    for v in reversed(series):
        if v is not None:
            found += 1
            if found == 2:
                return v
    return None


def _gain_20d(bars: list) -> Optional[float]:
    """
    Compute the 20-trading-day price gain as a fraction (e.g. 0.30 = +30 %).

    Returns None when fewer than 21 bars with valid closes are available.
    """
    closes = []
    for b in bars:
        c = b.get("close")
        if c is not None:
            try:
                closes.append(float(c))
            except (TypeError, ValueError):
                pass
    if len(closes) < 21:
        return None
    base = closes[-21]
    if base == 0:
        return None
    return (closes[-1] - base) / base


# ---------------------------------------------------------------------------
# Position sizing (SPEC F-07)
# ---------------------------------------------------------------------------

def position_sizing(entry: float, atr14: float, rr_ratio: float = 2.0) -> dict:
    """
    Compute ATR-based stop-loss and position sizing metrics.

    Args:
        entry:    Midpoint entry price (EMA20 is used as the reference level).
        atr14:    Current ATR(14) value.
        rr_ratio: Reward-to-risk multiplier (default 2.0 → 1:2 R:R).

    Returns dict with keys:
        entry_zone   {"low": float, "high": float}  — ±0.5 ATR around entry
        stop_loss    float
        risk_per_share float
        target       float
        rr_ratio     float
    """
    stop_distance = atr14 * 1.5
    half_atr = atr14 * 0.5
    return {
        "entry_zone": {
            "low": round(entry - half_atr, 2),
            "high": round(entry + half_atr, 2),
        },
        "stop_loss": round(entry - stop_distance, 2),
        "risk_per_share": round(stop_distance, 2),
        "target": round(entry + stop_distance * rr_ratio, 2),
        "rr_ratio": rr_ratio,
    }


# ---------------------------------------------------------------------------
# Condition builders
# ---------------------------------------------------------------------------

def _cond(label: str, met: bool, detail: str) -> dict:
    return {"label": label, "met": met, "detail": detail}


def _build_conditions(
    latest_close: float,
    ema20: Optional[float],
    ema50: Optional[float],
    rsi: Optional[float],
    macd_hist_latest: Optional[float],
    macd_hist_prev: Optional[float],
    volume_ratio: Optional[float],
    score: Optional[float],
    prev_score: Optional[float],
    gain_20d: Optional[float],
) -> list:
    """
    Build the full condition checklist that the UI renders.

    Each entry: {"label": str, "met": bool, "detail": str}
    """
    conds = []

    # --- Score condition ---
    if score is not None:
        conds.append(_cond(
            "Serenity Score >= 70",
            score >= 70,
            f"current: {score:.1f}",
        ))
    else:
        conds.append(_cond("Serenity Score >= 70", False, "no scorecard available"))

    # --- EMA20 condition ---
    if ema20 is not None:
        conds.append(_cond(
            "Price above EMA20",
            latest_close > ema20,
            f"close: {latest_close:.2f}, EMA20: {ema20:.2f}",
        ))
    else:
        conds.append(_cond("Price above EMA20", False, "EMA20 not available"))

    # --- RSI < 65 (BUY zone) ---
    if rsi is not None:
        conds.append(_cond(
            "RSI < 65",
            rsi < 65,
            f"RSI: {rsi:.1f}" + ("" if rsi < 65 else " — elevated, wait for pullback"),
        ))
    else:
        conds.append(_cond("RSI < 65", False, "RSI not available"))

    # --- Volume ratio ---
    if volume_ratio is not None:
        conds.append(_cond(
            "Volume Ratio > 1.5x avg",
            volume_ratio > 1.5,
            f"{volume_ratio:.2f}x 20-day avg",
        ))
    else:
        conds.append(_cond("Volume Ratio > 1.5x avg", False, "volume data not available"))

    # --- MACD golden cross (histogram crossed above 0) ---
    if macd_hist_latest is not None and macd_hist_prev is not None:
        golden_cross = macd_hist_latest > 0 and macd_hist_prev <= 0
        bullish_hist = macd_hist_latest > 0
        conds.append(_cond(
            "MACD bullish (histogram > 0)",
            bullish_hist,
            f"histogram: {macd_hist_latest:.4f}" + (" [fresh crossover]" if golden_cross else ""),
        ))
    elif macd_hist_latest is not None:
        conds.append(_cond(
            "MACD bullish (histogram > 0)",
            macd_hist_latest > 0,
            f"histogram: {macd_hist_latest:.4f}",
        ))
    else:
        conds.append(_cond("MACD bullish (histogram > 0)", False, "MACD not available"))

    # --- Price above EMA50 ---
    if ema50 is not None:
        conds.append(_cond(
            "Price above EMA50",
            latest_close > ema50,
            f"close: {latest_close:.2f}, EMA50: {ema50:.2f}",
        ))
    else:
        conds.append(_cond("Price above EMA50", False, "EMA50 not available"))

    # --- RSI in 50-70 (HOLD zone) ---
    if rsi is not None:
        in_hold_zone = 50 <= rsi <= 70
        conds.append(_cond(
            "RSI in hold zone [50, 70]",
            in_hold_zone,
            f"RSI: {rsi:.1f}",
        ))
    else:
        conds.append(_cond("RSI in hold zone [50, 70]", False, "RSI not available"))

    # --- RSI < 40 (EXIT trigger) ---
    if rsi is not None:
        conds.append(_cond(
            "RSI < 40 (exit trigger)",
            rsi < 40,
            f"RSI: {rsi:.1f}",
        ))
    else:
        conds.append(_cond("RSI < 40 (exit trigger)", False, "RSI not available"))

    # --- Score drop > 15 (EXIT trigger) ---
    if prev_score is not None and score is not None:
        drop = prev_score - score
        conds.append(_cond(
            "Score dropped > 15 pts",
            drop > 15,
            f"current: {score:.0f}, previous: {prev_score:.0f}, drop: {drop:.0f}",
        ))
    else:
        conds.append(_cond(
            "Score dropped > 15 pts",
            False,
            "no previous score to compare" if score is not None else "no scorecard available",
        ))

    # --- RSI > 75 (OVERBOUGHT trigger) ---
    if rsi is not None:
        conds.append(_cond(
            "RSI > 75 (overbought)",
            rsi > 75,
            f"RSI: {rsi:.1f}",
        ))
    else:
        conds.append(_cond("RSI > 75 (overbought)", False, "RSI not available"))

    # --- 20-day gain > 30 % (OVERBOUGHT trigger) ---
    if gain_20d is not None:
        pct = gain_20d * 100
        conds.append(_cond(
            "20-day gain > 30%",
            gain_20d > 0.30,
            f"{pct:.1f}% over last 20 trading days",
        ))
    else:
        conds.append(_cond("20-day gain > 30%", False, "insufficient price history"))

    return conds


# ---------------------------------------------------------------------------
# Signal determination
# ---------------------------------------------------------------------------

def _determine_signal(
    latest_close: float,
    ema20: Optional[float],
    ema50: Optional[float],
    rsi: Optional[float],
    macd_hist_latest: Optional[float],
    volume_ratio: Optional[float],
    score: Optional[float],
    prev_score: Optional[float],
    gain_20d: Optional[float],
) -> str:
    """Return the highest-priority matching signal string."""

    # --- OVERBOUGHT: RSI > 75 AND 20d gain > 30% ---
    if rsi is not None and rsi > 75 and gain_20d is not None and gain_20d > 0.30:
        return "OVERBOUGHT"

    # --- EXIT_ALERT: any one of: price < EMA50, RSI < 40, score drop > 15 ---
    exit_triggered = False
    if ema50 is not None and latest_close < ema50:
        exit_triggered = True
    if rsi is not None and rsi < 40:
        exit_triggered = True
    if score is not None and prev_score is not None and (prev_score - score) > 15:
        exit_triggered = True
    if exit_triggered:
        return "EXIT_ALERT"

    # --- BUY_TRIGGER: all BUY_WATCH conditions + volume_ratio > 1.5 + MACD hist > 0 ---
    score_ok = score is not None and score >= 70
    price_above_ema20 = ema20 is not None and latest_close > ema20
    rsi_ok = rsi is not None and rsi < 65
    vol_ok = volume_ratio is not None and volume_ratio > 1.5
    macd_ok = macd_hist_latest is not None and macd_hist_latest > 0

    if score_ok and price_above_ema20 and rsi_ok and vol_ok and macd_ok:
        return "BUY_TRIGGER"

    # --- BUY_WATCH: score >= 70 AND price > EMA20 (RSI condition informational) ---
    if score_ok and price_above_ema20:
        return "BUY_WATCH"

    # --- HOLD: price > EMA50, RSI in [50, 70], score >= 55 ---
    price_above_ema50 = ema50 is not None and latest_close > ema50
    rsi_hold = rsi is not None and 50 <= rsi <= 70
    score_hold = score is not None and score >= 55
    if price_above_ema50 and rsi_hold and score_hold:
        return "HOLD"

    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def evaluate_signal(
    latest_close: float,
    indicators: dict,
    score: Optional[float],
    bars: list,
    prev_score: Optional[float] = None,
    rr_ratio: float = 2.0,
) -> dict:
    """
    Compute a deterministic rule-based signal for a single symbol.

    Args:
        latest_close: Most recent closing price (from real stored data).
        indicators:   Output of indicators.compute_all(bars).
        score:        Serenity scorecard final_score (0–100), or None.
        bars:         Full OHLCV bar list (oldest-first) used for 20d gain.
        prev_score:   Previous scorecard score for score-drop detection.
        rr_ratio:     Reward-to-risk multiplier (default 2.0).

    Returns dict matching the /api/signal/<SYM> contract:
        symbol       — caller adds this
        signal       — signal string
        conditions   — list of {label, met, detail}
        entry_zone   — {low, high} or null
        stop_loss    — float or null
        risk_per_share — float or null
        target       — float or null
        rr_ratio     — float or null
        atr14        — float or null
        score        — float or null
        insufficient_data — bool
    """
    ema20_series = indicators.get("ema20", [])
    ema50_series = indicators.get("ema50", [])
    rsi_series = indicators.get("rsi14", [])
    macd_series = indicators.get("macd", [])
    atr14 = indicators.get("atr14")
    volume_ratio = indicators.get("volume_ratio")

    ema20 = _latest(ema20_series)
    ema50 = _latest(ema50_series)
    rsi = _latest(rsi_series)
    macd_hist_latest = _latest([m.get("histogram") for m in macd_series]) if macd_series else None
    macd_hist_prev = _prev([m.get("histogram") for m in macd_series]) if macd_series else None

    gain_20d = _gain_20d(bars)

    # Determine insufficient_data: need EMA50 + RSI + ATR at minimum
    insufficient_data = (ema50 is None or rsi is None or atr14 is None)

    if insufficient_data:
        # Build a minimal conditions list so the UI still renders
        conditions = _build_conditions(
            latest_close, ema20, ema50, rsi,
            macd_hist_latest, macd_hist_prev,
            volume_ratio, score, prev_score, gain_20d,
        )
        return {
            "signal": "NEUTRAL",
            "conditions": conditions,
            "entry_zone": None,
            "stop_loss": None,
            "risk_per_share": None,
            "target": None,
            "rr_ratio": None,
            "atr14": atr14,
            "score": score,
            "insufficient_data": True,
        }

    signal = _determine_signal(
        latest_close, ema20, ema50, rsi,
        macd_hist_latest, volume_ratio,
        score, prev_score, gain_20d,
    )

    conditions = _build_conditions(
        latest_close, ema20, ema50, rsi,
        macd_hist_latest, macd_hist_prev,
        volume_ratio, score, prev_score, gain_20d,
    )

    # Position sizing — use EMA20 as reference entry level
    if ema20 is not None and atr14 is not None and atr14 > 0:
        sizing = position_sizing(ema20, atr14, rr_ratio)
    else:
        sizing = {
            "entry_zone": None,
            "stop_loss": None,
            "risk_per_share": None,
            "target": None,
            "rr_ratio": None,
        }

    return {
        "signal": signal,
        "conditions": conditions,
        "entry_zone": sizing["entry_zone"],
        "stop_loss": sizing["stop_loss"],
        "risk_per_share": sizing["risk_per_share"],
        "target": sizing["target"],
        "rr_ratio": sizing["rr_ratio"],
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "score": score,
        "insufficient_data": False,
    }
