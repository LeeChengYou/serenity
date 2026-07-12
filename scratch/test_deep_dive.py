# -*- coding: utf-8 -*-
"""
個股深度研究報告（d-R1~d-R7）驗收測試（規格：docs/REQUIREMENTS_AI_MARKET.md d-驗收 1~7）

執行：PYTHONIOENCODING=utf-8 python scratch/test_deep_dive.py

原則：
  - 每個技術數字在測試內獨立重算（不 import deep_dive 內部函式對自己）
  - LLM 全用假 server / Stub，不打真 Ollama、不打真 Gemini
  - DB 只讀副本，絕不寫入正式 DB
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SERENITY_NO_DOTENV", "1")

# DB 來源：環境變數 SERENITY_DB_SRC 或主 repo 絕對路徑（worktree data/ 無真 DB）
_MAIN_DB = r"C:\Users\Jeff\OneDrive\桌面\git_repo\serenity\data\serenity.sqlite"
_DB_SRC = os.environ.get("SERENITY_DB_SRC", _MAIN_DB)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    mark = "  OK " if ok else "! FAIL"
    print(f"{mark}  {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    return ok


def approx(a, b, rel=1e-6):
    """數值近似比較（相對誤差 rel 以內）。"""
    if a is None or b is None:
        return False
    fa, fb = float(a), float(b)
    denom = max(abs(fb), 1e-12)
    return abs(fa - fb) / denom <= rel


def finish():
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print()
    print("=" * 70)
    print(f"Deep Dive Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


# ---------------------------------------------------------------------------
# 假 LLM HTTP server
# ---------------------------------------------------------------------------

def _make_llm_handler(response_content: str):
    body = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": response_content}}]
    }).encode("utf-8")

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"models": []}).encode())

    return _H


def _start_server(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


# ---------------------------------------------------------------------------
# 獨立重算工具（不 import deep_dive 內部函式）
# ---------------------------------------------------------------------------

def _ref_rsi14(closes):
    """Wilder RSI-14（對應 agent_arena._calc_rsi14）。"""
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:14]) / 14
    avg_l = sum(losses[:14]) / 14
    for i in range(14, len(gains)):
        avg_g = (avg_g * 13 + gains[i]) / 14
        avg_l = (avg_l * 13 + losses[i]) / 14
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def _ref_ema(closes, period):
    """EMA（對應 agent_arena._calc_ema）。"""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return round(ema, 4)


def _ref_atr14(rows_asc):
    """Wilder ATR-14（獨立實作，對應規格）。"""
    trs = []
    prev_close = None
    for r in rows_asc:
        c = r["close"]; h = r["high"]; lo = r["low"]
        if c is None:
            prev_close = c
            continue
        if h is None or lo is None:
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
    atr = sum(trs[:14]) / 14.0
    for t in trs[14:]:
        atr = (atr * 13 + t) / 14.0
    return atr


def _ref_ann_vol(closes):
    """年化波動率（獨立實作）。"""
    if len(closes) < 2:
        return None
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
    window = rets[-120:] if len(rets) >= 120 else rets
    if len(window) < 30:
        return None
    n = len(window)
    mean = sum(window) / n
    var = sum((v - mean) ** 2 for v in window) / (n - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def _ref_max_drawdown(closes):
    """最大回撤（正值%，獨立實作）。"""
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


def _ref_swing_lows(closes_60, n=3):
    """Swing lows（獨立實作）。"""
    swings = []
    for i in range(2, len(closes_60) - 2):
        c = closes_60[i]
        if c < closes_60[i-1] and c < closes_60[i-2] and c < closes_60[i+1] and c < closes_60[i+2]:
            swings.append((i, c))
    swings.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in swings[:n]]


def _ref_swing_highs(closes_60, n=3):
    """Swing highs（獨立實作）。"""
    swings = []
    for i in range(2, len(closes_60) - 2):
        c = closes_60[i]
        if c > closes_60[i-1] and c > closes_60[i-2] and c > closes_60[i+1] and c > closes_60[i+2]:
            swings.append((i, c))
    swings.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in swings[:n]]


# ---------------------------------------------------------------------------
# 主測試
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("Deep Dive Acceptance Test")
    print("=" * 70)

    # ── import deep_dive ──────────────────────────────────────────────────
    print("\n[P0] import serenity.services.deep_dive")
    try:
        from serenity.services.deep_dive import (
            deep_dive_payload, deep_dive_report, migrate,
            technical_summary_text, backfill_report_outcomes,
        )
    except Exception as exc:
        check("import deep_dive", False, repr(exc))
        finish()
        return
    check("import deep_dive OK", True)

    # ── DB sandbox ───────────────────────────────────────────────────────
    print("\n[setup] sandbox DB copy")
    tmpdir = Path(tempfile.mkdtemp(prefix="dd_test_"))
    test_db = tmpdir / "test.sqlite"
    src_con = sqlite3.connect(_DB_SRC)
    dst_con = sqlite3.connect(str(test_db))
    src_con.backup(dst_con)
    src_con.close()
    dst_con.close()
    check("test DB 在 tempdir", str(test_db).startswith(str(tmpdir)))

    # arena.connect gives row_factory
    import agent_arena as arena
    con = arena.connect(str(test_db))
    arena.migrate(con)   # ensure arena tables exist in copy
    migrate(con)         # ensure deep_dive_reports table

    SYM = "NVDA"
    AS_OF = "2026-07-10"

    # ── Test 1: technical 每個數字獨立重算一致 ────────────────────────────
    print(f"\n[T1] technical 指標獨立驗算（{SYM} as_of={AS_OF}）")

    # 取 raw 資料
    rows_raw = con.execute(
        "SELECT date, close, high, low FROM prices "
        "WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT 250",
        (SYM, AS_OF),
    ).fetchall()
    rows_asc = list(reversed(rows_raw))
    closes = [r["close"] for r in rows_asc if r["close"] is not None]
    closes_60 = closes[-60:] if len(closes) >= 60 else closes

    exp_rsi14   = _ref_rsi14(closes)
    exp_ema20   = _ref_ema(closes, 20)
    exp_ema50   = _ref_ema(closes, 50)
    exp_ema200  = _ref_ema(closes, 200)
    exp_atr14   = _ref_atr14(rows_asc)
    exp_ann_vol = _ref_ann_vol(closes)
    exp_hi_60d  = max(closes_60) if closes_60 else None
    exp_lo_60d  = min(closes_60) if closes_60 else None
    exp_max_dd  = _ref_max_drawdown(closes)
    exp_chg20   = (closes[-1] / closes[-21] - 1.0) * 100.0 if len(closes) >= 21 else None
    exp_support = _ref_swing_lows(closes_60)
    exp_resist  = _ref_swing_highs(closes_60)

    p = deep_dive_payload(con, SYM, AS_OF)
    t = p["technical"]

    check("T1.rsi14 match",   t["rsi14"]   == exp_rsi14,
          f"got={t['rsi14']} exp={exp_rsi14}")
    check("T1.ema20 match",   t["ema20"]   == exp_ema20,
          f"got={t['ema20']} exp={exp_ema20}")
    check("T1.ema50 match",   t["ema50"]   == exp_ema50,
          f"got={t['ema50']} exp={exp_ema50}")
    check("T1.ema200 match",  t["ema200"]  == exp_ema200,
          f"got={t['ema200']} exp={exp_ema200}")
    check("T1.atr14 match (rel 1e-6)",
          approx(t["atr14"], exp_atr14),
          f"got={t['atr14']} exp={exp_atr14}")
    check("T1.ann_vol_pct match (rel 1e-6)",
          approx(t["ann_vol_pct"], exp_ann_vol),
          f"got={t['ann_vol_pct']} exp={exp_ann_vol}")
    check("T1.hi_60d match",  approx(t["hi_60d"], exp_hi_60d),
          f"got={t['hi_60d']} exp={exp_hi_60d}")
    check("T1.lo_60d match",  approx(t["lo_60d"], exp_lo_60d),
          f"got={t['lo_60d']} exp={exp_lo_60d}")
    check("T1.max_drawdown match (rel 1e-6)",
          approx(t["max_drawdown_1y_pct"], exp_max_dd),
          f"got={t['max_drawdown_1y_pct']} exp={exp_max_dd}")
    check("T1.chg_20d_pct match (rel 1e-6)",
          approx(t["chg_20d_pct"], exp_chg20),
          f"got={t['chg_20d_pct']} exp={exp_chg20}")
    check("T1.support_levels match",
          len(t["support_levels"]) == len(exp_support) and
          all(approx(a, b) for a, b in zip(t["support_levels"], exp_support)),
          f"got={t['support_levels']} exp={exp_support}")
    check("T1.resistance_levels match",
          len(t["resistance_levels"]) == len(exp_resist) and
          all(approx(a, b) for a, b in zip(t["resistance_levels"], exp_resist)),
          f"got={t['resistance_levels']} exp={exp_resist}")
    check("T1.n_days correct", t["n_days"] == len(rows_raw),
          f"got={t['n_days']} exp={len(rows_raw)}")

    # ── Test 2: events 驗算 ───────────────────────────────────────────────
    print(f"\n[T2] events 驗算（{SYM} — NVDA 近期事件 d10 窗口不完整 → 全排除）")
    ev = p["events"]
    # NVDA 五個事件都是 2026-07 最後 10 天，d10 窗口不完整 → n=0
    check("T2.NVDA positive.n == 0 (d10 window not complete)",
          ev["positive"]["n"] == 0)
    check("T2.NVDA insufficient == True (n=0 < 10)",
          ev["insufficient"] is True)

    # 用 MTSI 驗算事件日集合與 d5 報酬
    print(f"\n[T2b] events 驗算（MTSI — 有完整 forward 窗口的事件）")
    SYM2 = "MTSI"
    p2 = deep_dive_payload(con, SYM2, AS_OF)
    ev2 = p2["events"]

    # 獨立計算 MTSI 事件日集合
    tdays_raw = con.execute(
        "SELECT date FROM prices WHERE symbol=? AND date <= ? ORDER BY date",
        (SYM2, AS_OF),
    ).fetchall()
    tdays = [r["date"] for r in tdays_raw]
    td_idx = {d: i for i, d in enumerate(tdays)}
    td_set = set(tdays)

    news_raw = con.execute(
        """
        SELECT date(published_at) as day,
               SUM(CASE WHEN sentiment='Bullish' THEN 1 ELSE 0 END) as bull,
               SUM(CASE WHEN sentiment='Bearish' THEN 1 ELSE 0 END) as bear
        FROM news_sentiment
        WHERE symbol=? AND date(published_at) <= ?
        GROUP BY day
        """,
        (SYM2, AS_OF),
    ).fetchall()

    ref_pos_events = []
    ref_neg_events = []
    for r in news_raw:
        day = r["day"]
        bull = r["bull"] or 0
        bear = r["bear"] or 0
        is_pos = (bull >= 2 and bull > bear)
        is_neg = (bear > bull)
        if not is_pos and not is_neg:
            continue
        base_td = day if day in td_set else None
        if base_td is None:
            for td in tdays:
                if td > day:
                    base_td = td
                    break
        if base_td is None:
            continue
        idx = td_idx[base_td]
        if idx + 10 >= len(tdays):
            continue
        bc_row = con.execute(
            "SELECT close FROM prices WHERE symbol=? AND date=?", (SYM2, base_td)
        ).fetchone()
        if not bc_row or not bc_row["close"]:
            continue
        bc = bc_row["close"]
        d5_td = tdays[idx + 5]
        d5_row = con.execute(
            "SELECT close FROM prices WHERE symbol=? AND date=?", (SYM2, d5_td)
        ).fetchone()
        d5_ret = (d5_row["close"] / bc - 1.0) * 100.0 if d5_row and d5_row["close"] else None
        if is_pos:
            ref_pos_events.append({"d5_ret": d5_ret, "day": day})
        else:
            ref_neg_events.append({"d5_ret": d5_ret})

    check("T2b.positive.n == ref count",
          ev2["positive"]["n"] == len(ref_pos_events),
          f"got={ev2['positive']['n']} exp={len(ref_pos_events)}")

    # 驗 d5 mean
    if ref_pos_events:
        d5_vals = [e["d5_ret"] for e in ref_pos_events if e["d5_ret"] is not None]
        exp_d5_mean = sum(d5_vals) / len(d5_vals) if d5_vals else None
        check("T2b.positive.d5_mean_pct match (rel 1e-6)",
              approx(ev2["positive"].get("d5_mean_pct"), exp_d5_mean),
              f"got={ev2['positive'].get('d5_mean_pct')} exp={exp_d5_mean}")
    else:
        check("T2b.no positive events (ok if DB has none)", True)

    # forward 窗不完整的事件被排除（MTSI 2026-07-09 之後的日子 d10 超過 as_of）
    # 只要 n 等於我們獨立算的 ref 就是對的（上面已驗）

    # insufficient 依 n
    total_n = ev2["positive"]["n"] + ev2["negative"]["n"]
    check("T2b.insufficient == (total_n < 10)",
          ev2["insufficient"] == (total_n < 10),
          f"total_n={total_n}")

    # ── Test 3: valuation 缺值 None ───────────────────────────────────────
    print(f"\n[T3] valuation 與 DB 一致（{SYM}）")
    val = p["valuation"]
    frow = con.execute(
        "SELECT pe, forward_pe, revenue_growth_yoy, next_earnings_date "
        "FROM fundamentals WHERE symbol=?", (SYM,)
    ).fetchone()
    erow = con.execute(
        "SELECT target_low, target_median, target_mean, target_high, n_analysts, recommendation_key "
        "FROM analyst_estimates WHERE symbol=?", (SYM,)
    ).fetchone()

    check("T3.pe match",    approx(val["pe"], frow["pe"]) if frow else val["pe"] is None)
    check("T3.forward_pe",  approx(val["forward_pe"], frow["forward_pe"]) if frow else val["forward_pe"] is None)
    check("T3.target_median", approx(val["target_median"], erow["target_median"]) if erow else val["target_median"] is None)
    check("T3.n_analysts",  val["n_analysts"] == (erow["n_analysts"] if erow else None))
    check("T3.recommendation_key", val["recommendation_key"] == (erow["recommendation_key"] if erow else None))
    # upside_to_median_pct
    if erow and erow["target_median"] and p["close"] and p["close"] > 0:
        exp_upside = (erow["target_median"] / p["close"] - 1.0) * 100.0
        check("T3.upside_to_median_pct match (rel 1e-6)",
              approx(val["upside_to_median_pct"], exp_upside),
              f"got={val['upside_to_median_pct']} exp={exp_upside}")
    else:
        check("T3.upside_to_median_pct is None when data missing",
              val["upside_to_median_pct"] is None)

    # 缺值 → None（製造假 symbol）
    p_missing = deep_dive_payload(con, "XXXNOTEXIST", AS_OF)
    check("T3.no-price symbol → error key", "error" in p_missing)

    # ── Test 4: reference_levels ──────────────────────────────────────────
    print(f"\n[T4] reference_levels（{SYM}）")
    ref = p["reference_levels"]
    close_v = p["close"]
    atr_v = t["atr14"]

    if close_v is not None and atr_v is not None:
        exp_stop = close_v - 2.0 * atr_v
        check("T4.stop_loss = close − 2×ATR14",
              approx(ref["stop_loss"], exp_stop),
              f"got={ref['stop_loss']} exp={exp_stop}")
    else:
        check("T4.stop_loss None when atr missing", ref["stop_loss"] is None)

    check("T4.stop_loss_basis non-empty", bool(ref.get("stop_loss_basis")))
    check("T4.entry_zone_basis non-empty", bool(ref.get("entry_zone_basis")))
    check("T4.exit_zone_basis non-empty", bool(ref.get("exit_zone_basis")))

    # entry_zone = [最近支撐位, 支撐位 + 0.5×ATR]
    if t["support_levels"] and atr_v is not None:
        sup = t["support_levels"][0]
        exp_entry = [sup, sup + 0.5 * atr_v]
        entry = ref.get("entry_zone")
        check("T4.entry_zone[0] = support", approx(entry[0], exp_entry[0]),
              f"got={entry[0]} exp={exp_entry[0]}")
        check("T4.entry_zone[1] = support + 0.5*atr", approx(entry[1], exp_entry[1]),
              f"got={entry[1]} exp={exp_entry[1]}")
    else:
        check("T4.entry_zone None when support/atr missing", ref["entry_zone"] is None)

    # exit_zone 組成（最近壓力位 + target_median）
    exit_z = ref.get("exit_zone")
    if t["resistance_levels"] or val.get("target_median"):
        check("T4.exit_zone is list", isinstance(exit_z, list))
        if exit_z:
            check("T4.exit_zone[0] <= exit_zone[1]", exit_z[0] <= exit_z[1])
    else:
        check("T4.exit_zone None when both missing", exit_z is None)

    # atr 缺 → stop_loss None 驗証（用合成 payload）
    fake_p = {
        "technical": {"support_levels": [], "resistance_levels": [], "atr14": None},
        "valuation": {"target_median": None},
        "events": {},
    }
    from serenity.services.deep_dive import _calc_reference_levels
    ref_no_atr = _calc_reference_levels(100.0, None, [], [], None)
    check("T4.stop_loss None when atr=None", ref_no_atr["stop_loss"] is None)
    check("T4.entry_zone None when atr=None", ref_no_atr["entry_zone"] is None)

    # ── Test 5: as_of 參數（抽 hi_60d 驗證） ─────────────────────────────
    print("\n[T5] as_of 參數：過去日期 → 只用截止日以前資料")
    OLD_AS_OF = "2026-01-10"
    p_old = deep_dive_payload(con, SYM, OLD_AS_OF)
    if "error" in p_old:
        check("T5.old as_of has data", False, p_old["error"])
    else:
        t_old = p_old["technical"]
        # 獨立重算 hi_60d @ OLD_AS_OF
        old_rows = con.execute(
            "SELECT close FROM prices WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT 60",
            (SYM, OLD_AS_OF),
        ).fetchall()
        old_closes = [r["close"] for r in reversed(old_rows) if r["close"] is not None]
        exp_old_hi = max(old_closes) if old_closes else None
        check("T5.hi_60d at old as_of matches SQL",
              approx(t_old["hi_60d"], exp_old_hi),
              f"got={t_old['hi_60d']} exp={exp_old_hi}")
        # 確認 as_of 欄位正確
        check("T5.as_of field == OLD_AS_OF", p_old["as_of"] == OLD_AS_OF)
        # 確認不含 2026-07 的數據（current hi_60d 應不同）
        if exp_old_hi and t["hi_60d"]:
            check("T5.old hi_60d != current hi_60d",
                  not approx(t_old["hi_60d"], t["hi_60d"]))

    # ── Test 6: deep_dive_report with fake LLM server ─────────────────────
    print("\n[T6] deep_dive_report：假 LLM server 下 narrative 落庫 + LLM 失敗不拋例外")
    NARRATIVE_TEXT = "這是測試 narrative 內容。本報告僅供模擬研究，非投資建議。"
    srv, port = _start_server(_make_llm_handler(NARRATIVE_TEXT))

    # 設定假 base_url 讓 call_local_llm 打到假 server（透過 env var LOCAL_LLM_BASE_URL）
    os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{port}"

    result = deep_dive_report(con, SYM, backend="local", as_of=AS_OF)
    check("T6.narrative returned", result.get("narrative") == NARRATIVE_TEXT,
          f"got={result.get('narrative')!r}")

    # 確認落庫
    report_row = con.execute(
        "SELECT * FROM deep_dive_reports WHERE symbol=? ORDER BY id DESC LIMIT 1",
        (SYM,),
    ).fetchone()
    check("T6.report saved to DB", report_row is not None)
    if report_row:
        check("T6.report.narrative correct",
              report_row["narrative"] == NARRATIVE_TEXT)
        check("T6.report.backend = 'local'", report_row["backend"] == "local")
        check("T6.report.outcome_7d is None", report_row["outcome_7d"] is None)

    # 報告列表完整
    reports_resp = con.execute(
        "SELECT * FROM deep_dive_reports WHERE symbol=? ORDER BY created_at DESC",
        (SYM,),
    ).fetchall()
    check("T6.reports list >=1", len(reports_resp) >= 1)

    # LLM 失敗 → 回 numeric + error 不拋例外
    srv.shutdown()
    # 打到不存在 port
    os.environ["LOCAL_LLM_BASE_URL"] = "http://127.0.0.1:19999"
    result_fail = deep_dive_report(con, SYM, backend="local", as_of=AS_OF)
    check("T6.LLM fail returns dict (not exception)", isinstance(result_fail, dict))
    check("T6.LLM fail has error key", "error" in result_fail)
    check("T6.LLM fail has close key (numeric present)",
          result_fail.get("close") is not None)

    # LLM 失敗 → 不落庫（不累積空 narrative 報告列）
    n_before_fail = con.execute(
        "SELECT COUNT(*) FROM deep_dive_reports WHERE narrative IS NULL"
    ).fetchone()[0]
    check("T6.LLM fail does NOT persist empty-narrative row",
          n_before_fail == 0, f"null-narrative rows={n_before_fail}")

    # backend 非法 → 僅在 API 層 400（此處 deep_dive_report 返回 error 欄）
    result_bad = deep_dive_report(con, SYM, backend="badbackend", as_of=AS_OF)
    check("T6.bad backend returns error", "error" in result_bad)
    n_after_bad = con.execute(
        "SELECT COUNT(*) FROM deep_dive_reports WHERE narrative IS NULL"
    ).fetchone()[0]
    check("T6.bad backend does NOT persist row", n_after_bad == 0)

    # ── Test 7: 會診 prompt 含 technical 區塊標記 ────────────────────────
    print("\n[T7] 會診 prompt 含 technical 區塊標記（technical_summary_text + fund_pool）")

    # 直接測試 technical_summary_text
    txt = technical_summary_text(p)
    check("T7.technical_summary_text non-empty", bool(txt.strip()))
    check("T7.contains '技術結構摘要'", "技術結構摘要" in txt)
    check("T7.contains 'RSI14'", "RSI14" in txt)
    check("T7.contains '止損參考'", "止損參考" in txt)

    # error payload → 回空字串
    txt_err = technical_summary_text({"error": "no data"})
    check("T7.error payload → empty string", txt_err == "")

    # 測試 fund_pool.run_consult prompt 含 technical 區塊（使用 StubConsultBackend）
    import fund_pool as fp
    fp.migrate(con)

    # 建一個測試資金池
    pool_id = fp.create_pool(con, "dd-test-pool-7", 3000.0, "2026-01-01T00:00:00")

    # 確保 NVDA 的 domain agent 存在
    # 用 StubConsultBackend 攔截 opine prompt
    class _InspectStub(fp.ConsultBackend):
        def __init__(self):
            self.opine_prompts = []

        def opine(self, agent_id, prompt):
            self.opine_prompts.append((agent_id, prompt))
            return {"stance": "neutral", "confidence": 0.5, "opinion": "test"}

        def synthesize(self, prompt):
            return "test summary"

    stub = _InspectStub()

    # 先建 arena agent（ensure domain agents exist for test）
    arena.migrate(con)
    try:
        con.execute(
            "INSERT OR IGNORE INTO agents (id, domain, style_seed, backend, status, relaunches, hwm, created_at) "
            "VALUES ('semis-momentum', 'semis', 'momentum', 'gemini', 'active', 0, 3000.0, '2026-01-01T00:00:00')",
        )
        con.commit()
    except Exception:
        pass

    try:
        fp.run_consult(
            con, pool_id, "請評估 NVDA 是否值得買入？", SYM,
            ["semis-momentum"], AS_OF, stub,
        )
        # 驗收：opine_prompts 中含 technical 區塊標記
        all_prompts = " ".join(pr for _, pr in stub.opine_prompts)
        check("T7.consult prompt contains '技術結構摘要'",
              "技術結構摘要" in all_prompts,
              "技術結構摘要 block not found in opine prompt")
    except Exception as exc:
        check("T7.run_consult OK", False, repr(exc))

    con.close()

    # ── Test d2: backfill_report_outcomes ─────────────────────────────────
    print("\n[d2] backfill_report_outcomes — outcome_7d 回填")

    # 用全新 tempfile DB（不依賴真 DB 的 NVDA 資料）
    d2_tmpdir = Path(tempfile.mkdtemp(prefix="dd_d2_test_"))
    d2_db = d2_tmpdir / "test.sqlite"
    d2_con = sqlite3.connect(str(d2_db))
    d2_con.row_factory = sqlite3.Row
    d2_con.execute("PRAGMA journal_mode=WAL")

    # 建立最小 schema
    d2_con.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL,
            UNIQUE(symbol, date)
        );
        CREATE TABLE IF NOT EXISTS deep_dive_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, as_of TEXT NOT NULL,
            close REAL, entry_lo REAL, entry_hi REAL,
            exit_lo REAL, exit_hi REAL, stop_loss REAL,
            narrative TEXT, backend TEXT NOT NULL DEFAULT 'local',
            created_at TEXT NOT NULL, outcome_7d REAL
        );
    """)
    d2_con.commit()

    SYM_D2 = "TSYM"
    AS_OF_D2 = "2026-01-10"
    BASE_CLOSE = 100.0

    # 插入 8 個交易日（as_of + 7 個之後）
    trading_days_d2 = [
        ("2026-01-10", 100.0),
        ("2026-01-11", 101.0),
        ("2026-01-12", 102.0),
        ("2026-01-13", 103.0),
        ("2026-01-14", 104.0),
        ("2026-01-15", 105.0),
        ("2026-01-16", 106.0),
        ("2026-01-17", 107.0),  # 第 7 交易日 after as_of
    ]
    for d, c in trading_days_d2:
        d2_con.execute(
            "INSERT OR IGNORE INTO prices (symbol, date, close) VALUES (?, ?, ?)",
            (SYM_D2, d, c),
        )
    d2_con.commit()

    # 插入一筆舊報告（as_of=2026-01-10，close=100.0，outcome_7d=NULL）
    d2_con.execute(
        """INSERT INTO deep_dive_reports
           (symbol, as_of, close, backend, created_at, outcome_7d)
           VALUES (?, ?, ?, 'local', '2026-01-10T00:00:00', NULL)""",
        (SYM_D2, AS_OF_D2, BASE_CLOSE),
    )
    d2_con.commit()

    # d2-驗收 1：回填值 = 獨立重算（第 7 交易日 close / 基準 close − 1）
    # 第 7 交易日 after as_of = 2026-01-17，close=107.0
    exp_outcome = 107.0 / BASE_CLOSE - 1.0
    filled_count = backfill_report_outcomes(d2_con)
    check("d2-驗收1：回填筆數=1", filled_count == 1, f"got={filled_count}")
    row_d2 = d2_con.execute(
        "SELECT outcome_7d FROM deep_dive_reports WHERE symbol=?", (SYM_D2,)
    ).fetchone()
    check("d2-驗收1：outcome_7d 非 NULL", row_d2 and row_d2["outcome_7d"] is not None)
    if row_d2 and row_d2["outcome_7d"] is not None:
        check("d2-驗收1：outcome_7d 值正確（rel 1e-6）",
              approx(row_d2["outcome_7d"], exp_outcome),
              f"got={row_d2['outcome_7d']} exp={exp_outcome}")

    # d2-驗收 2a：不足 7 交易日的列維持 NULL
    SYM_D2B = "TSYM2"
    AS_OF_D2B = "2026-01-15"
    d2_con.execute(
        "INSERT OR IGNORE INTO prices (symbol, date, close) VALUES (?, ?, ?)",
        (SYM_D2B, "2026-01-15", 50.0),
    )
    d2_con.execute(
        "INSERT OR IGNORE INTO prices (symbol, date, close) VALUES (?, ?, ?)",
        (SYM_D2B, "2026-01-16", 51.0),  # 只有 1 日，不足 7
    )
    d2_con.execute(
        """INSERT INTO deep_dive_reports
           (symbol, as_of, close, backend, created_at, outcome_7d)
           VALUES (?, ?, 50.0, 'local', '2026-01-15T00:00:00', NULL)""",
        (SYM_D2B, AS_OF_D2B),
    )
    d2_con.commit()
    backfill_report_outcomes(d2_con)
    row_d2b = d2_con.execute(
        "SELECT outcome_7d FROM deep_dive_reports WHERE symbol=?", (SYM_D2B,)
    ).fetchone()
    check("d2-驗收2a：不足7日 → outcome_7d 維持 NULL",
          row_d2b and row_d2b["outcome_7d"] is None)

    # d2-驗收 2b：close 與 prices 皆缺基準 → 維持 NULL 不拋例外
    SYM_D2C = "TSYM3"
    AS_OF_D2C = "2026-01-10"
    try:
        d2_con.execute(
            """INSERT INTO deep_dive_reports
               (symbol, as_of, close, backend, created_at, outcome_7d)
               VALUES (?, ?, NULL, 'local', '2026-01-10T00:00:00', NULL)""",
            (SYM_D2C, AS_OF_D2C),
        )
        d2_con.commit()
        backfill_report_outcomes(d2_con)
        row_d2c = d2_con.execute(
            "SELECT outcome_7d FROM deep_dive_reports WHERE symbol=?", (SYM_D2C,)
        ).fetchone()
        check("d2-驗收2b：close/prices 皆缺 → NULL 不拋例外",
              row_d2c and row_d2c["outcome_7d"] is None)
    except Exception as exc:
        check("d2-驗收2b：不拋例外", False, repr(exc))

    # d2-驗收 3：冪等（跑兩次，第二次回傳 0）
    filled2 = backfill_report_outcomes(d2_con)
    check("d2-驗收3：冪等第二次回傳 0", filled2 == 0, f"got={filled2}")
    row_d2_again = d2_con.execute(
        "SELECT outcome_7d FROM deep_dive_reports WHERE symbol=?", (SYM_D2,)
    ).fetchone()
    check("d2-驗收3：值不變",
          row_d2_again and approx(row_d2_again["outcome_7d"], exp_outcome))

    # d2-驗收 4：fund_pool daily 路徑會呼叫到（monkeypatch 計數）
    import fund_pool as fp
    calls = []
    _orig_bro = None
    try:
        import serenity.services.deep_dive as _dd_mod
        _orig_bro = _dd_mod.backfill_report_outcomes
        _dd_mod.backfill_report_outcomes = lambda con: calls.append(1) or 0

        # 模擬 cmd_daily 的 deep_dive 呼叫路徑
        # fund_pool.cmd_daily 用 importlib 動態 import，我們直接驗底層匯入路徑
        # 建立 tempfile DB 副本執行 cmd_daily
        import argparse as _ap
        d2_fp_db = d2_tmpdir / "fp_test.sqlite"
        d2_fp_con = sqlite3.connect(str(d2_fp_db))
        d2_fp_con.row_factory = sqlite3.Row
        import agent_arena as _aa
        _aa.migrate(d2_fp_con)
        fp.migrate(d2_fp_con)
        # 確保 prices 表存在並有一筆資料（agent_arena.migrate 不建 prices）
        d2_fp_con.executescript("""
            CREATE TABLE IF NOT EXISTS prices (
                symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
                close REAL, volume REAL, UNIQUE(symbol, date)
            );
        """)
        d2_fp_con.execute(
            "INSERT OR IGNORE INTO prices (symbol, date, close) VALUES ('AAPL','2026-01-10',150.0)"
        )
        d2_fp_con.commit()

        _fp_args = _ap.Namespace(db=str(d2_fp_db))
        fp.cmd_daily(_fp_args)
        check("d2-驗收4：fund_pool daily 路徑呼叫 backfill_report_outcomes", len(calls) == 1,
              f"calls={calls}")
    except Exception as exc:
        check("d2-驗收4：monkeypatch 執行", False, repr(exc))
    finally:
        if _orig_bro is not None:
            import serenity.services.deep_dive as _dd_mod2
            _dd_mod2.backfill_report_outcomes = _orig_bro

    d2_con.close()
    try:
        d2_fp_con.close()
    except Exception:
        pass

    finish()


if __name__ == "__main__":
    main()
