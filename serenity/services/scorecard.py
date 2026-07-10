"""
serenity/services/scorecard.py
scorecard 生成邏輯（從 route_post_api 651-844 行抽出的具名函式）
這是規格唯一允許的「抽函式」。
"""
import json
import os
from datetime import datetime

from ..db import db
from ..gemini import call_gemini


def generate_scorecard(symbol: str) -> dict:
    """
    為指定股票代號生成供應鏈瓶頸記分卡，寫入 DB，回傳結果。
    POST /api/scorecard/generate/<SYM> 的核心邏輯。
    """
    try:
        con = db()
        try:
            tweets = [r[0] for r in con.execute(
                "select text from mentions where symbol=? order by mentioned_at desc limit 20",
                (symbol,)
            ).fetchall()]
        finally:
            con.close()

        tweets_text = "\n".join([f"- {t}" for t in tweets]) if tweets else \
            "本機資料庫無相關貼文，請以您的知識庫分析該公司。"

        system_prompt = (
            f"你是一個資深晶片與科技半導體供應鏈研究專家。你的任務是分析 {symbol} 這家公司，遵循 serenity-skill 的卡點/瓶頸評級準則，產生一份定性的「供應鏈瓶頸記分卡」。\n"
            "請嚴格依據事實或您的專業產業知識進行評估，絕不捏造子虛烏有的事實。\n"
            "必須返回 JSON 格式，包含以下欄位：\n"
            "{\n"
            "  \"company\": \"公司官方名稱\",\n"
            "  \"market\": \"公司掛牌市場 (例如 US Stock / Taiwan Stock)\",\n"
            "  \"factors\": {\n"
            "    \"demand_inflection\": 0-5 評級,\n"
            "    \"architecture_coupling\": 0-5 評級,\n"
            "    \"chokepoint_severity\": 0-5 評級,\n"
            "    \"supplier_concentration\": 0-5 評級,\n"
            "    \"expansion_difficulty\": 0-5 評級,\n"
            "    \"evidence_quality\": 0-5 評級,\n"
            "    \"valuation_disconnect\": 0-5 評級,\n"
            "    \"catalyst_timing\": 0-5 評級\n"
            "  },\n"
            "  \"penalties\": {\n"
            "    \"dilution_financing\": 0-5 評級,\n"
            "    \"governance\": 0-5 評級,\n"
            "    \"geopolitics\": 0-5 評級,\n"
            "    \"liquidity\": 0-5 評級,\n"
            "    \"hype_risk\": 0-5 評級,\n"
            "    \"accounting_quality\": 0-5 評級,\n"
            "    \"cyclicality\": 0-5 評級,\n"
            "    \"alternative_design_risk\": 0-5 評級\n"
            "  },\n"
            "  \"evidence\": [\n"
            "    {\"claim\": \"事實證據陳述一\", \"source\": \"事實來源\", \"strength\": \"strong/medium/weak之一\"}\n"
            "  ],\n"
            "  \"what_could_weaken_view\": [\n"
            "    \"可能削弱此瓶頸看法的因素一\",\n"
            "    \"可能削弱此瓶頸看法的因素二\"\n"
            "  ]\n"
            "}\n"
            "注意：請用台灣繁體中文寫所有內容。"
        )

        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        res_data = call_gemini(
            model_name=model_name,
            contents=[{"role": "user", "parts": [{"text": f"請對個股 {symbol} 進行定性供應鏈瓶頸分析。本機資料庫相關貼文如下：\n{tweets_text}"}]}],
            system_instruction=system_prompt,
            temperature=0.3,
            response_mime_type="application/json",
            task_class="batch",
        )

        reply_text = res_data['candidates'][0]['content']['parts'][0]['text']
        card_data = json.loads(reply_text)

        WEIGHTS = {
            "demand_inflection": 15,
            "architecture_coupling": 10,
            "chokepoint_severity": 15,
            "supplier_concentration": 12,
            "expansion_difficulty": 12,
            "evidence_quality": 15,
            "valuation_disconnect": 11,
            "catalyst_timing": 10,
        }
        PENALTY_MULTIPLIER = 2.0

        factors = card_data.get("factors", {})
        penalties = card_data.get("penalties", {})

        factor_details = {}
        total = 0.0
        for key, weight in WEIGHTS.items():
            rating = float(factors.get(key, 0))
            rating = max(0.0, min(5.0, rating))
            points = rating / 5.0 * weight
            factor_details[key] = {"rating": rating, "weight": weight, "points": round(points, 2)}
            total += points

        penalty_details = {}
        penalty_total = 0.0
        for key, val in penalties.items():
            rating = float(val)
            rating = max(0.0, min(5.0, rating))
            points = rating * PENALTY_MULTIPLIER
            penalty_details[key] = {"rating": rating, "points": round(points, 2)}
            penalty_total += points

        final_score = max(0.0, min(100.0, total - penalty_total))

        if final_score >= 85:
            verdict = "Top research priority"
        elif final_score >= 70:
            verdict = "High research priority"
        elif final_score >= 55:
            verdict = "Worth tracking"
        else:
            verdict = "Early lead or low priority"

        now_str = datetime.now().isoformat()
        model_name_used = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        con = db()
        try:
            # Task D (SPEC F-03): archive existing scorecard to history
            # BEFORE overwriting, so the timeline is always append-only.
            existing = con.execute(
                "select final_score, verdict, factors_json, penalties_json "
                "from scorecards where symbol=?", (symbol,)
            ).fetchone()
            if existing:
                con.execute(
                    "insert into scorecard_history "
                    "(symbol, final_score, verdict, factors_json, penalties_json, model_used, created_at) "
                    "values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        symbol,
                        existing[0],
                        existing[1],
                        existing[2],
                        existing[3],
                        model_name_used,
                        now_str,
                    ),
                )

            con.execute("""
                insert into scorecards (
                    symbol, company, market, final_score, verdict,
                    raw_factor_points, penalty_points,
                    factors_json, penalties_json, evidence_json, kill_switches_json,
                    updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol) do update set
                    company=excluded.company,
                    market=excluded.market,
                    final_score=excluded.final_score,
                    verdict=excluded.verdict,
                    raw_factor_points=excluded.raw_factor_points,
                    penalty_points=excluded.penalty_points,
                    factors_json=excluded.factors_json,
                    penalties_json=excluded.penalties_json,
                    evidence_json=excluded.evidence_json,
                    kill_switches_json=excluded.kill_switches_json,
                    updated_at=excluded.updated_at
            """, (
                symbol,
                card_data.get("company", symbol),
                card_data.get("market", "-"),
                round(final_score, 2),
                verdict,
                round(total, 2),
                round(penalty_total, 2),
                json.dumps(factor_details, ensure_ascii=False),
                json.dumps(penalty_details, ensure_ascii=False),
                json.dumps(card_data.get("evidence", []), ensure_ascii=False),
                json.dumps(card_data.get("what_could_weaken_view", []), ensure_ascii=False),
                now_str
            ))
            con.commit()
        finally:
            con.close()

        return {"success": True, "final_score": round(final_score, 2)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        safe_msg = str(e)
        if "key=" in safe_msg.lower() or "api_key" in safe_msg.lower() or "goog-api-key" in safe_msg.lower():
            safe_msg = "Internal API request error (credentials hidden for security)"
        return {"error": safe_msg}
