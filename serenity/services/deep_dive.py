"""
serenity/services/deep_dive.py
個股深度研究報告（d-R1 ~ d-R2 + migrate）

規格：docs/REQUIREMENTS_AI_MARKET.md § (d)
原則：數字由 python 確定性計算；LLM 只做綜合解讀；零捏造；零 look-ahead。
"""
from __future__ import annotations

import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 重用 agent_arena 的指標函式（import 方式與 fund_pool.py 一致）
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

import agent_arena as _arena

_calc_rsi14 = _arena._calc_rsi14
_calc_ema   = _arena._calc_ema


# ---------------------------------------------------------------------------
# migrate — 冪等建立 deep_dive_reports 表
# ---------------------------------------------------------------------------

def migrate(con: sqlite3.Connection) -> None:
    """冪等建立 deep_dive_reports 表。"""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS deep_dive_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            as_of       TEXT NOT NULL,
            close       REAL,
            entry_lo    REAL,
            entry_hi    REAL,
            exit_lo     REAL,
            exit_hi     REAL,
            stop_loss   REAL,
            narrative   TEXT,
            backend     TEXT NOT NULL DEFAULT 'local',
            created_at  TEXT NOT NULL,
            outcome_7d  REAL
        );
    """)
    con.commit()


# ---------------------------------------------------------------------------
# 內部計算工具
# ---------------------------------------------------------------------------

def _stdev_sample(vals: list[float]) -> float:
    """母體樣本標準差（n-1）。"""
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))


def _calc_atr14(rows: list) -> float | None:
    """
    Wilder ATR-14（規格 d-R1）。
    rows: sqlite3.Row list，含 close/high/low 欄；high/low 為 NULL 的日子跳過。
    有效 TR < 14 → None。
    """
    trs: list[float] = []
    prev_close: float | None = None
    for r in rows:
        c = r["close"]
        h = r["high"]
        lo = r["low"]
        if c is None:
            prev_close = c
            continue
        if h is None or lo is None:
            # high/low NULL：跳過此日 TR（但 prev_close 可更新）
            prev_close = c
            continue
        if prev_close is None:
            prev_close = c
            continue
        tr = max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = c

    if len(trs) < 14:
        return None
    # 前 14 個 TR 簡單平均起始
    atr = sum(trs[:14]) / 14.0
    for t in trs[14:]:
        atr = (atr * 13 + t) / 14.0
    return atr


def _calc_ann_vol(closes: list[float]) -> float | None:
    """
    最近 120 個日報酬樣本標準差 × √252 × 100（%）。
    不足 30 筆 → None。
    """
    if len(closes) < 2:
        return None
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
    # 用最近 120 個日報酬
    window = rets[-120:] if len(rets) >= 120 else rets
    if len(window) < 30:
        return None
    return _stdev_sample(window) * math.sqrt(252) * 100.0


def _calc_max_drawdown(closes: list[float]) -> float:
    """最大回撤（正值百分比）。closes 為時序列。"""
    if not closes:
        return 0.0
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100.0


def _swing_lows(closes: list[float], n: int = 3) -> list[float]:
    """
    最近 60 交易日 close 中嚴格低於前後各 2 日的 swing low，
    由近到遠取最近 n 個。
    """
    swings: list[tuple[int, float]] = []
    for i in range(2, len(closes) - 2):
        c = closes[i]
        if c < closes[i - 1] and c < closes[i - 2] and c < closes[i + 1] and c < closes[i + 2]:
            swings.append((i, c))
    # 由近到遠（index 大 = 近）
    swings.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in swings[:n]]


def _swing_highs(closes: list[float], n: int = 3) -> list[float]:
    """最近 60 交易日 close 中嚴格高於前後各 2 日的 swing high，由近到遠取最近 n 個。"""
    swings: list[tuple[int, float]] = []
    for i in range(2, len(closes) - 2):
        c = closes[i]
        if c > closes[i - 1] and c > closes[i - 2] and c > closes[i + 1] and c > closes[i + 2]:
            swings.append((i, c))
    swings.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in swings[:n]]


# ---------------------------------------------------------------------------
# d-R1 deep_dive_payload
# ---------------------------------------------------------------------------

def deep_dive_payload(
    con: sqlite3.Connection,
    symbol: str,
    as_of: str | None = None,
) -> dict:
    """
    確定性計算個股技術結構、事件研究、估值錨、參考位。
    全部計算只用 date <= as_of 的資料（零 look-ahead）。
    as_of 缺省 = 該 symbol prices 最大 date；無價格 → {"error": ...}。
    """
    symbol = symbol.upper()

    # 1. 確定 as_of
    if as_of is None:
        row = con.execute(
            "SELECT MAX(date) FROM prices WHERE symbol=?", (symbol,)
        ).fetchone()
        if not row or not row[0]:
            return {"error": f"查無 {symbol} 的價格資料"}
        as_of = row[0]
    else:
        # 確認有資料
        row = con.execute(
            "SELECT MAX(date) FROM prices WHERE symbol=? AND date <= ?", (symbol, as_of)
        ).fetchone()
        if not row or not row[0]:
            return {"error": f"查無 {symbol} 在 {as_of} 以前的價格資料"}

    # 2. 取最近 250 交易日資料（含 high/low）
    price_rows = con.execute(
        "SELECT date, close, high, low FROM prices "
        "WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT 250",
        (symbol, as_of),
    ).fetchall()
    if not price_rows:
        return {"error": f"查無 {symbol} 的價格資料"}

    price_rows_asc = list(reversed(price_rows))  # 時序升序（舊→新）
    n_days = len(price_rows_asc)
    closes = [r["close"] for r in price_rows_asc if r["close"] is not None]
    current_close = closes[-1] if closes else None

    # 3. technical
    rsi14   = _calc_rsi14(closes)
    ema20   = _calc_ema(closes, 20)
    ema50   = _calc_ema(closes, 50)
    ema200  = _calc_ema(closes, 200)
    atr14   = _calc_atr14(price_rows_asc)
    ann_vol = _calc_ann_vol(closes)

    # hi_60d / lo_60d：最近 60 交易日 close 極值
    closes_60 = closes[-60:] if len(closes) >= 60 else closes
    hi_60d = max(closes_60) if closes_60 else None
    lo_60d = min(closes_60) if closes_60 else None

    # swing low/high（最近 60 交易日，嚴格定義）
    support_levels    = _swing_lows(closes_60)
    resistance_levels = _swing_highs(closes_60)

    # max_drawdown_1y_pct（250 日）
    max_dd = _calc_max_drawdown(closes) if closes else 0.0

    # chg_20d_pct
    chg_20d: float | None = None
    if len(closes) >= 21:
        base = closes[-21]
        if base and base > 0:
            chg_20d = (closes[-1] / base - 1.0) * 100.0

    technical: dict = {
        "n_days":             n_days,
        "rsi14":              rsi14,
        "ema20":              ema20,
        "ema50":              ema50,
        "ema200":             ema200,
        "atr14":              atr14,
        "ann_vol_pct":        ann_vol,
        "hi_60d":             hi_60d,
        "lo_60d":             lo_60d,
        "support_levels":     support_levels,
        "resistance_levels":  resistance_levels,
        "max_drawdown_1y_pct": max_dd,
        "chg_20d_pct":        chg_20d,
    }

    # 4. events（news_sentiment d-R1 事件研究）
    events = _calc_events(con, symbol, as_of)

    # 5. valuation
    valuation = _calc_valuation(con, symbol, current_close)

    # 6. reference_levels
    reference_levels = _calc_reference_levels(
        current_close, atr14, support_levels, resistance_levels,
        valuation.get("target_median"),
    )

    return {
        "symbol":           symbol,
        "as_of":            as_of,
        "close":            current_close,
        "technical":        technical,
        "events":           events,
        "valuation":        valuation,
        "reference_levels": reference_levels,
    }


def _calc_events(con: sqlite3.Connection, symbol: str, as_of: str) -> dict:
    """
    事件研究（規格 d-R1 events 節）。
    日聚合 bull/bear 筆數；正面事件日 = bull≥2 且 bull>bear；負面 = bear>bull。
    forward 窗不完整（d10 close > as_of 對應交易日）的事件被排除。
    """
    # 取出所有交易日序列（≤ as_of）
    td_rows = con.execute(
        "SELECT date FROM prices WHERE symbol=? AND date <= ? ORDER BY date",
        (symbol, as_of),
    ).fetchall()
    trading_days = [r["date"] for r in td_rows]
    td_set = set(trading_days)
    td_idx = {d: i for i, d in enumerate(trading_days)}

    # 日聚合 news_sentiment（≤ as_of）
    news_rows = con.execute(
        """
        SELECT date(published_at) as day,
               SUM(CASE WHEN sentiment='Bullish' THEN 1 ELSE 0 END) as bull,
               SUM(CASE WHEN sentiment='Bearish' THEN 1 ELSE 0 END) as bear
        FROM news_sentiment
        WHERE symbol=? AND date(published_at) <= ?
        GROUP BY day
        """,
        (symbol, as_of),
    ).fetchall()

    pos_events: list[dict] = []
    neg_events: list[dict] = []

    for row in news_rows:
        day = row["day"]
        bull = row["bull"] or 0
        bear = row["bear"] or 0

        is_positive = (bull >= 2 and bull > bear)
        is_negative = (bear > bull)
        if not is_positive and not is_negative:
            continue

        # 找事件基準：事件日或其後第一個交易日
        base_td = None
        if day in td_set:
            base_td = day
        else:
            # 找事件日之後第一個交易日
            for td in trading_days:
                if td > day:
                    base_td = td
                    break
        if base_td is None:
            continue

        base_idx = td_idx[base_td]
        base_close_row = con.execute(
            "SELECT close FROM prices WHERE symbol=? AND date=?",
            (symbol, base_td),
        ).fetchone()
        if not base_close_row or not base_close_row["close"]:
            continue
        base_close = base_close_row["close"]

        # forward 窗：d1/d5/d10 需完整（≤ as_of）
        def _fwd_close(offset: int) -> float | None:
            idx = base_idx + offset
            if idx >= len(trading_days):
                return None
            td = trading_days[idx]
            r = con.execute(
                "SELECT close FROM prices WHERE symbol=? AND date=?", (symbol, td)
            ).fetchone()
            return r["close"] if r and r["close"] is not None else None

        d10_close = _fwd_close(10)
        if d10_close is None:
            # forward 窗不完整，排除
            continue

        d1_close  = _fwd_close(1)
        d5_close  = _fwd_close(5)

        def _ret(c):
            if c is None or base_close == 0:
                return None
            return (c / base_close - 1.0) * 100.0

        evt = {
            "day": day,
            "base_close": base_close,
            "d1_ret": _ret(d1_close),
            "d5_ret": _ret(d5_close),
            "d10_ret": _ret(d10_close),
        }

        if is_positive:
            pos_events.append(evt)
        else:
            neg_events.append(evt)

    def _aggregate(evts: list[dict]) -> dict:
        n = len(evts)
        if n == 0:
            return {"n": 0, "d1_mean_pct": None, "d1_win_rate": None,
                    "d5_mean_pct": None, "d5_win_rate": None,
                    "d10_mean_pct": None, "d10_win_rate": None}

        def _mean(key):
            vals = [e[key] for e in evts if e[key] is not None]
            return sum(vals) / len(vals) if vals else None

        def _win(key):
            vals = [e[key] for e in evts if e[key] is not None]
            if not vals:
                return None
            return sum(1 for v in vals if v > 0) / len(vals) * 100.0

        return {
            "n":             n,
            "d1_mean_pct":   _mean("d1_ret"),
            "d1_win_rate":   _win("d1_ret"),
            "d5_mean_pct":   _mean("d5_ret"),
            "d5_win_rate":   _win("d5_ret"),
            "d10_mean_pct":  _mean("d10_ret"),
            "d10_win_rate":  _win("d10_ret"),
        }

    pos_agg = _aggregate(pos_events)
    neg_agg = _aggregate(neg_events)
    total_n = pos_agg["n"] + neg_agg["n"]

    return {
        "positive":   pos_agg,
        "negative":   neg_agg,
        "insufficient": total_n < 10,
    }


def _calc_valuation(con: sqlite3.Connection, symbol: str, current_close: float | None) -> dict:
    """估值錨（規格 d-R1 valuation 節）。缺值 → None，不捏造。"""
    frow = con.execute(
        "SELECT pe, forward_pe, revenue_growth_yoy, next_earnings_date "
        "FROM fundamentals WHERE symbol=?", (symbol,)
    ).fetchone()

    erow = con.execute(
        "SELECT target_low, target_median, target_mean, target_high, n_analysts, recommendation_key "
        "FROM analyst_estimates WHERE symbol=?", (symbol,)
    ).fetchone()

    pe               = frow["pe"]              if frow else None
    forward_pe       = frow["forward_pe"]      if frow else None
    rev_growth       = frow["revenue_growth_yoy"] if frow else None
    next_earnings    = frow["next_earnings_date"]  if frow else None

    target_low       = erow["target_low"]       if erow else None
    target_median    = erow["target_median"]    if erow else None
    target_mean      = erow["target_mean"]      if erow else None
    target_high      = erow["target_high"]      if erow else None
    n_analysts       = erow["n_analysts"]       if erow else None
    recommendation   = erow["recommendation_key"] if erow else None

    upside = None
    if target_median is not None and current_close and current_close > 0:
        upside = (target_median / current_close - 1.0) * 100.0

    return {
        "pe":                    pe,
        "forward_pe":            forward_pe,
        "revenue_growth_yoy":    rev_growth,
        "next_earnings_date":    next_earnings,
        "target_low":            target_low,
        "target_median":         target_median,
        "target_mean":           target_mean,
        "target_high":           target_high,
        "n_analysts":            n_analysts,
        "recommendation_key":    recommendation,
        "upside_to_median_pct":  upside,
    }


def _calc_reference_levels(
    close: float | None,
    atr14: float | None,
    support_levels: list[float],
    resistance_levels: list[float],
    target_median: float | None,
) -> dict:
    """
    確定性參考位（規格 d-R1 reference_levels 節）。每個附 basis 說明。
    """
    # stop_loss = close − 2×atr14
    stop_loss: float | None = None
    stop_loss_basis = "close − 2×ATR14"
    if close is not None and atr14 is not None:
        stop_loss = close - 2.0 * atr14

    # entry_zone = [最近支撐位, 支撐位 + 0.5×atr14]
    entry_zone: list[float] | None = None
    entry_zone_basis = "最近 swing low 支撐位 ± 0.5×ATR14"
    if support_levels and atr14 is not None:
        sup = support_levels[0]  # 最近支撐位（由近到遠第一個）
        entry_zone = [sup, sup + 0.5 * atr14]
    elif support_levels and atr14 is None:
        entry_zone = None  # atr 缺 → None
    else:
        entry_zone = None

    # exit_zone = 最近壓力位與 target_median 可得者構成 [min, max]
    exit_zone: list[float] | None = None
    exit_zone_basis = "最近 swing high 壓力位 + 分析師目標中位數"
    candidates: list[float] = []
    if resistance_levels:
        candidates.append(resistance_levels[0])  # 最近壓力位
    if target_median is not None:
        candidates.append(target_median)
    if len(candidates) >= 2:
        exit_zone = [min(candidates), max(candidates)]
    elif len(candidates) == 1:
        exit_zone = [candidates[0], candidates[0]]
    else:
        exit_zone = None

    return {
        "stop_loss":       stop_loss,
        "stop_loss_basis": stop_loss_basis,
        "entry_zone":      entry_zone,
        "entry_zone_basis": entry_zone_basis,
        "exit_zone":       exit_zone,
        "exit_zone_basis": exit_zone_basis,
    }


# ---------------------------------------------------------------------------
# d-R2 deep_dive_report
# ---------------------------------------------------------------------------

def deep_dive_report(
    con: sqlite3.Connection,
    symbol: str,
    backend: str = "local",
    as_of: str | None = None,
) -> dict:
    """
    numeric payload → LLM 綜合 → 落庫 deep_dive_reports。
    LLM 失敗 → 回 numeric + error 欄（不拋例外）。
    """
    payload = deep_dive_payload(con, symbol, as_of)
    if "error" in payload:
        return payload

    migrate(con)  # 確保表存在

    # 組 LLM prompt
    prompt_text = _build_llm_prompt(symbol, payload)

    narrative: str | None = None
    error_msg: str | None = None

    if backend == "local":
        try:
            from ..llm_local import call_local_llm
            narrative = call_local_llm(
                messages=[{"role": "user", "content": prompt_text}],
                temperature=0.3,
            )
        except Exception as exc:
            error_msg = f"本地 LLM 失敗：{exc}"
    elif backend == "gemini":
        try:
            from ..gemini import call_gemini
            from ..config import get_setting
            model = get_setting("gemini_model") or "gemini-1.5-flash"
            resp = call_gemini(
                model_name=model,
                contents=[{"parts": [{"text": prompt_text}], "role": "user"}],
                system_instruction="你是股票研究助理，只做綜合解讀，不做投資建議。",
                temperature=0.3,
            )
            narrative = resp["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            error_msg = f"Gemini 呼叫失敗：{exc}"
    else:
        error_msg = f"backend 不合法：{backend!r}（合法值：'local'、'gemini'）"

    # 落庫
    now_iso = datetime.utcnow().isoformat()
    ref = payload.get("reference_levels", {})
    close = payload.get("close")
    entry_zone = ref.get("entry_zone")
    exit_zone  = ref.get("exit_zone")
    stop_loss  = ref.get("stop_loss")

    entry_lo = entry_zone[0] if entry_zone else None
    entry_hi = entry_zone[1] if entry_zone else None
    exit_lo  = exit_zone[0]  if exit_zone  else None
    exit_hi  = exit_zone[1]  if exit_zone  else None

    as_of_val = payload["as_of"]

    con.execute(
        """
        INSERT INTO deep_dive_reports
            (symbol, as_of, close, entry_lo, entry_hi, exit_lo, exit_hi,
             stop_loss, narrative, backend, created_at, outcome_7d)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (symbol, as_of_val, close, entry_lo, entry_hi,
         exit_lo, exit_hi, stop_loss, narrative, backend, now_iso),
    )
    con.commit()

    result = dict(payload)
    if narrative is not None:
        result["narrative"] = narrative
    if error_msg is not None:
        result["error"] = error_msg
    return result


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


def _build_llm_prompt(symbol: str, payload: dict) -> str:
    """
    組 LLM 綜合解讀 prompt。所有數字預先由 python 格式化（2 位小數）。
    """
    t   = payload.get("technical", {})
    ev  = payload.get("events", {})
    val = payload.get("valuation", {})
    ref = payload.get("reference_levels", {})
    close = payload.get("close")
    as_of = payload.get("as_of", "—")

    lines = [
        f"請根據以下 {symbol} 的量化數據做綜合解讀（繁體中文，約 300 字）。",
        f"資料截止日：{as_of}  收盤價：{_fmt_price(close)}",
        "",
        "## 技術結構",
        f"- RSI14：{t.get('rsi14') or '—'}",
        f"- EMA20：{_fmt_price(t.get('ema20'))}  EMA50：{_fmt_price(t.get('ema50'))}  EMA200：{_fmt_price(t.get('ema200'))}",
        f"- ATR14：{_fmt_price(t.get('atr14'))}",
        f"- 年化波動率：{_fmt_pct(t.get('ann_vol_pct'))}",
        f"- 近 60 日高：{_fmt_price(t.get('hi_60d'))}  近 60 日低：{_fmt_price(t.get('lo_60d'))}",
        f"- 近 250 日最大回撤：{_fmt_pct(t.get('max_drawdown_1y_pct'))}",
        f"- 近 20 日漲跌：{_fmt_pct(t.get('chg_20d_pct'))}",
    ]

    # 事件研究
    pos = ev.get("positive", {})
    neg = ev.get("negative", {})
    insuf = ev.get("insufficient", True)
    lines += [
        "",
        "## 事件研究",
        f"（{'樣本不足，僅供參考' if insuf else '樣本數足夠'}）",
        f"正面事件：{pos.get('n', 0)} 次  "
        f"D1 平均：{_fmt_pct(pos.get('d1_mean_pct'))}  "
        f"D5 平均：{_fmt_pct(pos.get('d5_mean_pct'))}  "
        f"D10 平均：{_fmt_pct(pos.get('d10_mean_pct'))}",
        f"負面事件：{neg.get('n', 0)} 次  "
        f"D1 平均：{_fmt_pct(neg.get('d1_mean_pct'))}  "
        f"D5 平均：{_fmt_pct(neg.get('d5_mean_pct'))}  "
        f"D10 平均：{_fmt_pct(neg.get('d10_mean_pct'))}",
    ]

    # 估值錨
    lines += [
        "",
        "## 估值錨",
        f"- PE：{_fmt_price(val.get('pe'))}  Forward PE：{_fmt_price(val.get('forward_pe'))}",
        f"- 營收成長（YoY）：{_fmt_pct(val.get('revenue_growth_yoy') and val['revenue_growth_yoy'] * 100)}",
        f"- 分析師目標：低 {_fmt_price(val.get('target_low'))}  中位 {_fmt_price(val.get('target_median'))}  "
        f"高 {_fmt_price(val.get('target_high'))}（{val.get('n_analysts') or '—'} 位分析師）",
        f"- 上行空間至中位數：{_fmt_pct(val.get('upside_to_median_pct'))}",
        f"- 評級：{val.get('recommendation_key') or '—'}",
        f"- 下次財報：{val.get('next_earnings_date') or '—'}",
    ]

    # 參考位
    entry_zone = ref.get("entry_zone")
    exit_zone  = ref.get("exit_zone")
    lines += [
        "",
        "## 參考位（確定性計算，非預測）",
        f"- 止損位：{_fmt_price(ref.get('stop_loss'))}（{ref.get('stop_loss_basis', '—')}）",
        f"- 進場區間：{_fmt_price(entry_zone[0] if entry_zone else None)} ~ "
        f"{_fmt_price(entry_zone[1] if entry_zone else None)}（{ref.get('entry_zone_basis', '—')}）",
        f"- 出場區間：{_fmt_price(exit_zone[0] if exit_zone else None)} ~ "
        f"{_fmt_price(exit_zone[1] if exit_zone else None)}（{ref.get('exit_zone_basis', '—')}）",
        "",
        "請綜合以上資料給出分析觀點，樣本不足時如實說明，結尾附上：「本報告僅供模擬研究，非投資建議。」",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# deep_dive_payload 轉文字（d-R5 會診整合用）
# ---------------------------------------------------------------------------

def technical_summary_text(payload: dict) -> str:
    """
    將 deep_dive_payload 的 technical + reference_levels 轉成精簡文字塊。
    供 run_consult 意見輪 prompt 使用。
    失敗（error payload）→ 回空字串（不阻擋會診）。
    """
    if "error" in payload or "technical" not in payload:
        return ""

    t   = payload.get("technical", {})
    ref = payload.get("reference_levels", {})
    sym = payload.get("symbol", "—")
    as_of = payload.get("as_of", "—")
    close = payload.get("close")

    entry_zone = ref.get("entry_zone")
    exit_zone  = ref.get("exit_zone")

    lines = [
        f"## 技術結構摘要（{sym}，截止 {as_of}，收盤 {_fmt_price(close)}）",
        f"RSI14={t.get('rsi14') or '—'}  EMA20={_fmt_price(t.get('ema20'))}  EMA50={_fmt_price(t.get('ema50'))}  EMA200={_fmt_price(t.get('ema200'))}",
        f"ATR14={_fmt_price(t.get('atr14'))}  年化波動={_fmt_pct(t.get('ann_vol_pct'))}",
        f"60日高低：{_fmt_price(t.get('hi_60d'))} / {_fmt_price(t.get('lo_60d'))}",
        f"20日報酬：{_fmt_pct(t.get('chg_20d_pct'))}  最大回撤(1y)：{_fmt_pct(t.get('max_drawdown_1y_pct'))}",
        f"支撐位：{[round(x,2) for x in t.get('support_levels',[])]}  壓力位：{[round(x,2) for x in t.get('resistance_levels',[])]}",
        f"止損參考：{_fmt_price(ref.get('stop_loss'))}",
        f"進場區間：{_fmt_price(entry_zone[0] if entry_zone else None)} ~ {_fmt_price(entry_zone[1] if entry_zone else None)}",
        f"出場區間：{_fmt_price(exit_zone[0] if exit_zone else None)} ~ {_fmt_price(exit_zone[1] if exit_zone else None)}",
    ]
    return "\n".join(lines)
