"""
serenity/services/regime.py
_last_non_none, _benchmark_block, regime_payload
（原 server.py 1607-1721 行）
"""
from datetime import datetime

from ..quant import _compute_ema


def _last_non_none(series: list):
    """Return the last non-None value in a series."""
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _benchmark_block(con, symbol: str):
    """EMA200 stance for one benchmark ETF: {"close","ema200","above"} or None."""
    try:
        closes = [r[0] for r in con.execute(
            "select close from prices where symbol=? and close is not null order by date",
            (symbol,),
        )]
        if len(closes) < 200 or _compute_ema is None:
            return None
        ema200 = _last_non_none(_compute_ema(closes, 200))
        if ema200 is None:
            return None
        return {
            "close": round(closes[-1], 4),
            "ema200": round(ema200, 4),
            "above": closes[-1] > ema200,
        }
    except Exception:
        return None


def regime_payload(con) -> dict:
    """
    GET /api/regime  (contract: REQUIREMENTS_V3.md R3-2)

    Regime rule (spec):
      bull:    SPY>EMA200 AND SOXX>EMA200 AND >50% of universe above EMA50
      bear:    SPY<EMA200 OR SOXX<EMA200*0.97
      neutral: everything else
      unknown: benchmark data missing/insufficient

    Returns:
      {"as_of","regime","spy":{close,ema200,above},"soxx":{...},"qqq":{...},
       "universe_above_ema50_pct","note"}
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    base = {
        "as_of": as_of,
        "regime": "unknown",
        "spy": None, "soxx": None, "qqq": None,
        "universe_above_ema50_pct": None,
        "note": "缺乏基準指數資料，無法判斷市場環境。請先執行 `python scripts/ingest.py benchmarks`。",
    }

    try:
        spy  = _benchmark_block(con, "SPY")
        soxx = _benchmark_block(con, "SOXX")
        qqq  = _benchmark_block(con, "QQQ")

        # Universe breadth: % of non-benchmark symbols above their EMA50
        univ_pct = None
        try:
            syms = [r[0] for r in con.execute(
                "select distinct symbol from prices "
                "where symbol not in ('SPY','SOXX','QQQ')"
            )]
            above = total = 0
            for sym in syms:
                closes = [r[0] for r in con.execute(
                    "select close from prices where symbol=? and close is not null order by date",
                    (sym,),
                )]
                if len(closes) < 50 or _compute_ema is None:
                    continue
                ema50 = _last_non_none(_compute_ema(closes, 50))
                if ema50 is None:
                    continue
                total += 1
                if closes[-1] > ema50:
                    above += 1
            if total >= 10:
                univ_pct = round(above / total, 4)
        except Exception as exc:
            print(f"[regime] universe breadth failed: {exc}")

        if spy is None or soxx is None:
            return {**base, "spy": spy, "soxx": soxx, "qqq": qqq,
                    "universe_above_ema50_pct": univ_pct}

        if not spy["above"] or soxx["close"] < soxx["ema200"] * 0.97:
            regime = "bear"
            note = ("空頭環境：大盤或半導體基準跌破長期均線。動能訊號（OVERBOUGHT）"
                    "在此環境未經驗證，買進建議信心度應下調。")
        elif spy["above"] and soxx["above"] and univ_pct is not None and univ_pct > 0.5:
            regime = "bull"
            note = ("多頭環境：基準指數站上 EMA200 且過半個股站上 EMA50。"
                    "既有樣本外驗證主要來自此環境。")
        else:
            regime = "neutral"
            note = ("中性/轉折環境：基準與個股廣度訊號不一致。"
                    "建議降低倉位並提高對訊號的懷疑度。")

        return {
            "as_of": as_of,
            "regime": regime,
            "spy": spy, "soxx": soxx, "qqq": qqq,
            "universe_above_ema50_pct": univ_pct,
            "note": note,
        }

    except Exception as exc:
        print(f"[regime_payload] error: {exc}")
        return base
