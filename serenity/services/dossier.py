"""
serenity/services/dossier.py
_RELIABILITY_NOTE, dossier_payload
（原 server.py 2373-2788 行）
"""
import json
import os
from datetime import datetime

from ..config import DB_PATH
from ..db import _table_exists
from ..gemini import call_gemini
from ..keypool import _key_manager
from ..quant import _compute_indicators, _quant_score
from .signal import signal_payload
from .regime import regime_payload
from .market import estimates_payload


_RELIABILITY_NOTE = (
    "Multi-window out-of-sample validation (21 cutoffs, fixed 30-day horizon, "
    "1541 observations) found: BUY_WATCH UNDERPERFORMS the universe (-4.8pp, "
    "n=35); chasing extended high-heat stocks is a confirmed drag (-5.2pp, "
    "n=30); EXIT_ALERT shows NO edge at scale (-0.9pp, n=488 — the earlier "
    "+13pp result was period noise); a pullback-entry variant looks promising "
    "(+71% win rate) but n=7 is insufficient to conclude. Observations overlap "
    "across windows, so effective sample sizes are smaller than stated. See "
    "docs/VALIDATION.md; reproduce with scripts/backtest_multiwindow.py. This "
    "is not investment advice."
)


def dossier_payload(con, symbol: str, refresh: bool = False) -> dict:
    """
    R-3: Build (or return cached) the /api/dossier/<SYM> response.

    Assembles all real evidence from SQLite, generates a Gemini manager_view
    narrative from ONLY that data, and caches the result in the `dossiers`
    table.  Pass refresh=True to bypass the cache and regenerate.

    Graceful degradation: if the Gemini call fails (no key, network error,
    parse error), manager_view is set to null and the full data dossier is
    still returned.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")

    # --- 1. Check cache (unless refresh requested) ---
    if not refresh:
        cached = con.execute(
            "select dossier_json from dossiers where symbol=?", (symbol,)
        ).fetchone()
        if cached:
            try:
                return json.loads(cached[0])
            except Exception:
                pass

    # --- 2. Fetch real OHLCV bars ---
    bars = [dict(r) for r in con.execute(
        "select date, open, high, low, close, volume "
        "from prices where symbol=? order by date",
        (symbol,),
    )]

    latest_close = None
    for b in reversed(bars):
        c = b.get("close")
        if c is not None:
            try:
                latest_close = float(c)
                break
            except (TypeError, ValueError):
                pass

    # --- 3. Compute indicators ---
    indicators = {}
    if bars:
        try:
            indicators = _compute_indicators(bars)
        except Exception:
            pass

    ema20_series = indicators.get("ema20", [])
    ema50_series = indicators.get("ema50", [])
    rsi_series = indicators.get("rsi14", [])
    atr14 = indicators.get("atr14")

    def _lat(series):
        for v in reversed(series):
            if v is not None:
                return v
        return None

    ema20 = _lat(ema20_series)
    ema50 = _lat(ema50_series)
    rsi = _lat(rsi_series)

    # --- 4. Quant score + components ---
    score = None
    score_source = None
    quant_components = {}
    sc_row = con.execute(
        "select final_score from scorecards where symbol=?", (symbol,)
    ).fetchone()
    if sc_row and sc_row[0] is not None:
        score = sc_row[0]
        score_source = "scorecard"

    if _quant_score is not None:
        try:
            q = _quant_score(DB_PATH, symbol)
            if q:
                quant_components = q.get("components", {})
                if score is None and q.get("score") is not None:
                    score = q["score"]
                    score_source = "quant"
        except Exception:
            pass

    quant_section = {
        "score": score,
        "source": score_source,
        "components": quant_components,
    }

    # --- 5. Technicals section ---
    trend = None
    if latest_close is not None and ema50 is not None:
        trend = "above EMA50" if latest_close > ema50 else "below EMA50"
    elif latest_close is not None and ema20 is not None:
        trend = "above EMA20" if latest_close > ema20 else "below EMA20"

    atr_pct = None
    if atr14 is not None and latest_close is not None and latest_close > 0:
        atr_pct = round(atr14 / latest_close * 100, 2)

    technicals_section = {
        "latest_close": latest_close,
        "trend": trend,
        "ema20": round(ema20, 4) if ema20 is not None else None,
        "ema50": round(ema50, 4) if ema50 is not None else None,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "atr_pct": atr_pct,
    }

    # --- 6. Signal section (reuse signal_payload internals) ---
    sig_data = signal_payload(con, symbol)
    signal_section = {
        "state": sig_data.get("signal", "NEUTRAL"),
        "key_conditions": [
            c for c in (sig_data.get("conditions") or []) if c.get("met")
        ],
        "entry_zone": sig_data.get("entry_zone"),
        "stop_loss": sig_data.get("stop_loss"),
        "target": sig_data.get("target"),
        "ema20_ref": sig_data.get("ema20_ref"),
    }

    # --- 7. Sentiment section ---
    sent_rows = con.execute(
        "select sentiment from news_sentiment where symbol=? "
        "order by published_at desc limit 100",
        (symbol,),
    ).fetchall()
    sentiment_section = None
    if sent_rows:
        bull = sum(1 for (s,) in sent_rows if s == "Bullish")
        bear = sum(1 for (s,) in sent_rows if s == "Bearish")
        tagged = bull + bear
        sentiment_section = {
            "stocktwits_bull_ratio": round(bull / tagged, 3) if tagged else None,
            "bull": bull,
            "bear": bear,
            "sample": tagged,
        }

    # --- 8. Scorecard summary ---
    sc_full = con.execute(
        "select symbol, final_score, verdict, factors_json from scorecards where symbol=?",
        (symbol,),
    ).fetchone()
    scorecard_section = None
    if sc_full:
        top_factors = []
        try:
            fd = json.loads(sc_full["factors_json"] or "{}")
            top_factors = sorted(
                [{"name": k, "points": v.get("points", 0)} for k, v in fd.items()],
                key=lambda x: x["points"],
                reverse=True,
            )[:3]
        except Exception:
            pass
        scorecard_section = {
            "final_score": sc_full["final_score"],
            "verdict": sc_full["verdict"],
            "top_factors": top_factors,
        }

    # --- 9. Evidence: top 3 tweets by engagement ---
    evidence_rows = con.execute(
        """select m.text, t.url, t.favorite_count, t.reply_count,
                  t.retweet_count, m.mentioned_at
           from mentions m join tweets t on t.tweet_id=m.tweet_id
           where m.symbol=?
           order by (coalesce(t.favorite_count,0)
                     + 2*coalesce(t.retweet_count,0)
                     + coalesce(t.reply_count,0)) desc
           limit 3""",
        (symbol,),
    ).fetchall()
    evidence_section = [
        {
            "text": (r["text"] or "")[:400],
            "url": r["url"],
            "date": (r["mentioned_at"] or "")[:10],
            "engagement": (
                (r["favorite_count"] or 0)
                + 2 * (r["retweet_count"] or 0)
                + (r["reply_count"] or 0)
            ),
        }
        for r in evidence_rows
    ]

    # --- 9b. Fundamentals section (R2-4) ---
    fundamentals_section = None
    try:
        if _table_exists(con, "fundamentals"):
            frow = con.execute(
                """select pe, forward_pe, eps_ttm, revenue_growth_yoy,
                          gross_margin, market_cap, next_earnings_date, updated_at
                   from fundamentals where symbol=?""",
                (symbol,),
            ).fetchone()
            if frow:
                fundamentals_section = dict(frow)
    except Exception as exc:
        print(f"[Dossier] fundamentals fetch failed for {symbol}: {exc}")

    # --- 9c. News section (R2-4) ---
    news_section = {"items": [], "macro": []}
    try:
        if _table_exists(con, "news"):
            sym_rows = con.execute(
                """select title, source, url, published_at, summary
                   from news
                   where scope='symbol'
                     and (symbols like ? or symbols like ? or symbols like ? or symbols like ?)
                     and published_at >= datetime('now', '-7 days')
                   order by published_at desc
                   limit 10""",
                (
                    f'["{symbol}"]',
                    f'"{symbol}",%',
                    f'%,"{symbol}",%',
                    f'%,"{symbol}"]',
                ),
            ).fetchall()
            news_section["items"] = [dict(r) for r in sym_rows]

            macro_rows = con.execute(
                """select title, source, url, published_at, summary
                   from news
                   where scope='macro'
                     and published_at >= datetime('now', '-3 days')
                   order by published_at desc
                   limit 5""",
            ).fetchall()
            news_section["macro"] = [dict(r) for r in macro_rows]
    except Exception as exc:
        print(f"[Dossier] news fetch failed for {symbol}: {exc}")

    # --- 9d. Regime section (R3-2) ---
    regime_section = None
    try:
        regime_section = regime_payload(con)
    except Exception as exc:
        print(f"[Dossier] regime fetch failed: {exc}")

    # --- 9e. Analyst estimates section (R3-3) ---
    estimates_section = None
    try:
        estimates_section = estimates_payload(con, symbol)
        # Only include if at least one non-null field exists (besides symbol/updated_at)
        _est_vals = [
            estimates_section.get(k) for k in (
                "target_mean", "n_analysts", "recommendation_key",
                "eps_estimate_current_q", "revision_direction"
            )
        ]
        if not any(v is not None for v in _est_vals):
            estimates_section = None
    except Exception as exc:
        print(f"[Dossier] estimates fetch failed for {symbol}: {exc}")

    # --- 10. Build Gemini prompt and call for manager_view ---
    manager_view = None
    if _key_manager.has_any_key():
        try:
            current_regime = (regime_section or {}).get("regime", "unknown")
            prompt_data = {
                "symbol": symbol,
                "as_of": as_of,
                "latest_close": latest_close,
                "quant_score": score,
                "score_source": score_source,
                "quant_components": quant_components,
                "technicals": technicals_section,
                "signal_state": signal_section["state"],
                "key_conditions_met": [c["label"] for c in signal_section["key_conditions"]],
                "entry_zone": signal_section["entry_zone"],
                "stop_loss": signal_section["stop_loss"],
                "target": signal_section["target"],
                "sentiment": sentiment_section,
                "scorecard": scorecard_section,
                "top_evidence_snippets": [e["text"][:200] for e in evidence_section],
                "fundamentals": fundamentals_section,
                "recent_symbol_news_titles": [
                    n["title"] for n in news_section["items"]
                ],
                "recent_macro_news_titles": [
                    n["title"] for n in news_section["macro"]
                ],
                # R3-2: regime context
                "market_regime": current_regime,
                "regime_data": {
                    "spy": (regime_section or {}).get("spy"),
                    "soxx": (regime_section or {}).get("soxx"),
                    "universe_above_ema50_pct": (regime_section or {}).get("universe_above_ema50_pct"),
                    "note": (regime_section or {}).get("note"),
                } if regime_section else None,
                # R3-3: analyst estimates
                "analyst_estimates": {
                    k: estimates_section.get(k)
                    for k in (
                        "target_mean", "target_median", "n_analysts",
                        "recommendation_key", "recommendation_mean",
                        "eps_estimate_current_q", "eps_estimate_next_y",
                        "up_revisions_30d", "down_revisions_30d",
                        "revision_direction", "target_vs_price",
                    )
                } if estimates_section else None,
            }

            # Bear-regime prompt instruction (zh-TW, injected into system prompt)
            _bear_warning = ""
            if current_regime == "bear":
                _bear_warning = (
                    "\n\n【熊市警告 — 必須執行】目前 SPY 市場環境判斷為「熊市（bear）」："
                    "SPY 收盤價與 EMA50 均低於 EMA200。在此環境下，你必須："
                    "（1）將 conviction 調降至 LOW 或最多 MEDIUM（除非有壓倒性多頭證據）；"
                    "（2）在 bear_case 與 position_guidance 中明確加入動能下行風險警語（以繁體中文撰寫）；"
                    "（3）recommendation 不得輸出 ACCUMULATE，優先考慮 WATCH 或 AVOID。"
                )

            system_instruction = (
                "You are a senior quantitative investment analyst generating a structured manager-view dossier. "
                "CRITICAL RULES:\n"
                "1. You MUST use ONLY the data provided in the user's JSON payload. Do NOT introduce any external facts, "
                "market knowledge, or opinions not present in the payload.\n"
                "2. If the data is insufficient, say so explicitly in the thesis field.\n"
                "3. All numbers you cite must come verbatim from the payload.\n"
                "4. Return ONLY valid JSON with exactly these fields: "
                "thesis (string), bull_case (string), bear_case (string), "
                "conviction (one of: LOW, MEDIUM, HIGH), "
                "recommendation (one of: AVOID, WATCH, ACCUMULATE, HOLD, REDUCE), "
                "position_guidance (string — ATR-based, anchored to latest close from the payload).\n"
                "5. Write in English. Be concise. Do not invent facts."
                + _bear_warning
            )

            user_text = (
                f"Generate a manager-view dossier for ${symbol} using ONLY the following real data:\n\n"
                + json.dumps(prompt_data, ensure_ascii=False, indent=2)
            )

            model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            res_data = call_gemini(
                model_name=model_name,
                contents=[{"role": "user", "parts": [{"text": user_text}]}],
                system_instruction=system_instruction,
                temperature=0.25,
                response_mime_type="application/json",
                task_class="interactive",
            )
            reply_text = res_data["candidates"][0]["content"]["parts"][0]["text"]
            mv = json.loads(reply_text)
            # Defensive: ensure required keys exist
            manager_view = {
                "thesis": str(mv.get("thesis") or ""),
                "bull_case": str(mv.get("bull_case") or ""),
                "bear_case": str(mv.get("bear_case") or ""),
                "conviction": str(mv.get("conviction") or "LOW"),
                "recommendation": str(mv.get("recommendation") or "WATCH"),
                "position_guidance": str(mv.get("position_guidance") or ""),
            }
        except Exception as exc:
            print(f"[Dossier] Gemini call failed for {symbol}: {exc}")
            manager_view = None

    # --- 11. Assemble full dossier ---
    dossier = {
        "symbol": symbol,
        "as_of": as_of,
        "quant": quant_section,
        "technicals": technicals_section,
        "signal": signal_section,
        "sentiment": sentiment_section,
        "scorecard": scorecard_section,
        "evidence": evidence_section,
        "fundamentals": fundamentals_section,
        "news": news_section,
        "regime": regime_section,
        "estimates": estimates_section,
        "manager_view": manager_view,
        "reliability_note": _RELIABILITY_NOTE,
    }

    # --- 12. Cache in dossiers table ---
    try:
        con.execute(
            """insert into dossiers (symbol, dossier_json, created_at)
               values (?, ?, ?)
               on conflict(symbol) do update set
                   dossier_json=excluded.dossier_json,
                   created_at=excluded.created_at""",
            (symbol, json.dumps(dossier, ensure_ascii=False), datetime.now().isoformat()),
        )
        con.commit()
    except Exception as exc:
        print(f"[Dossier] Cache write failed for {symbol}: {exc}")

    return dossier
