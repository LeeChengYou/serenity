#!/usr/bin/env python3
"""Integrated Serenity Research Scorer.

Combines quantitative X corpus signal stats and qualitative supply-chain bottleneck scoring.
"""
from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

# Setup paths to import from both skills
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / 'skills' / 'serenity-stock-scorer' / 'scripts'))
sys.path.append(str(PROJECT_ROOT / 'skills' / 'serenity-skill' / 'scripts'))

try:
    from score_serenity_stock import score_symbol, find_db
except ImportError:
    print("Error: Could not import score_serenity_stock.py. Ensure skills are copied.", file=sys.stderr)
    sys.exit(1)

try:
    from serenity_scorecard import score as score_bottleneck, to_markdown as bottleneck_to_markdown
except ImportError:
    print("Error: Could not import serenity_scorecard.py. Ensure skills are copied.", file=sys.stderr)
    sys.exit(1)


def save_scorecard_to_db(db_path: Path, symbol: str, res: dict) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("""
            create table if not exists scorecards (
                symbol text primary key,
                company text,
                market text,
                final_score real,
                verdict text,
                raw_factor_points real,
                penalty_points real,
                factors_json text,
                penalties_json text,
                evidence_json text,
                kill_switches_json text,
                updated_at text
            )
        """)
        now = datetime.now().isoformat()
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
            res.get("company", ""),
            res.get("market", ""),
            res.get("final_score", 0.0),
            res.get("verdict", ""),
            res.get("raw_factor_points", 0.0),
            res.get("penalty_points", 0.0),
            json.dumps(res.get("factor_details", {}), ensure_ascii=False),
            json.dumps(res.get("penalty_details", {}), ensure_ascii=False),
            json.dumps(res.get("evidence", []), ensure_ascii=False),
            json.dumps(res.get("kill_switches", []), ensure_ascii=False),
            now
        ))
        con.commit()
        print(f"[SUCCESS] Scorecard metrics for ${symbol} successfully saved to SQLite database.")
    except Exception as e:
        print(f"Error saving scorecard to database: {e}", file=sys.stderr)
    finally:
        con.close()


def generate_suggested_scorecard_template(symbol: str, quantitative_result: dict) -> dict:
    """Create a suggested scorecard template pre-filled with quantitative signals."""
    metrics = quantitative_result.get('metrics', {})
    mentions = metrics.get('mentions', 0)
    topic_hits = metrics.get('topic_hits', {})
    
    # Heuristics to estimate starting factors from X corpus signals
    demand_rating = 0
    if mentions > 0:
        # Boost demand rating if there are active topic hits in infrastructure/semi/optical
        infra_hits = topic_hits.get('ai_infra_neocloud', 0) + topic_hits.get('optical_photonics_networking', 0)
        semi_hits = topic_hits.get('semi_materials_packaging', 0)
        if infra_hits > 3 or semi_hits > 3:
            demand_rating = 4.5
        elif mentions > 10:
            demand_rating = 4.0
        else:
            demand_rating = 3.0
            
    # Estimate hype risk from caution hits
    caution_hits = metrics.get('marker_hits', {}).get('caution', 0)
    hype_rating = min(5.0, caution_hits * 1.0 + (1.5 if mentions > 30 else 0))

    evidence_rating = min(5.0, 1.0 + (mentions * 0.15))

    return {
        "ticker": symbol,
        "company": f"{symbol} (Auto-Generated)",
        "market": "US/Taiwan/A-share/HK",
        "notes": "Quantitative clues pre-filled. Refine ratings (0-5) manually.",
        "factors": {
            "demand_inflection": demand_rating,
            "architecture_coupling": 0,
            "chokepoint_severity": 0,
            "supplier_concentration": 0,
            "expansion_difficulty": 0,
            "evidence_quality": round(evidence_rating, 1),
            "valuation_disconnect": 0,
            "catalyst_timing": 3.0 if mentions > 0 else 0
        },
        "penalties": {
            "dilution_financing": 0,
            "governance": 0,
            "geopolitics": 0,
            "liquidity": 0,
            "hype_risk": round(hype_rating, 1),
            "accounting_quality": 0,
            "cyclicality": 0,
            "alternative_design_risk": 0
        },
        "evidence": [
            {
                "claim": f"X Corpus mentions count: {mentions}",
                "source": "Local Serenity SQLite Ledger",
                "strength": "medium" if mentions > 5 else "weak"
            }
        ],
        "what_could_weaken_view": [
            "Market narrative shifts away from this ticker",
            "Alternative supply chain configurations bypass this chokepoint"
        ]
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Integrated Serenity Quantitative and Qualitative Scorer.')
    parser.add_argument('symbol', help='Stock symbol / ticker')
    parser.add_argument('--db', help='Path to serenity.sqlite database')
    parser.add_argument('--scorecard', help='Path to a qualitative bottleneck scorecard JSON file')
    parser.add_argument('--export-template', action='store_true', help='Export a pre-filled scorecard JSON template based on X signals')
    args = parser.parse_args()

    symbol = args.symbol.upper().lstrip('$').strip()
    
    # 1. Fetch Quantitative X Sentiment Signals
    try:
        db_path = find_db(args.db)
    except SystemExit:
        db_path = None
        
    quant_result = None
    if db_path and db_path.exists():
        quant_result = score_symbol(db_path, symbol)
    else:
        print("Warning: data/serenity.sqlite not found. Skipping quantitative database query.", file=sys.stderr)

    # 2. Handle Template Export Action
    if args.export_template:
        if not quant_result:
            print("Error: Quantitative results are required to generate template.", file=sys.stderr)
            sys.exit(1)
        template = generate_suggested_scorecard_template(symbol, quant_result)
        print(json.dumps(template, ensure_ascii=False, indent=2))
        return

    # 3. Print Unified Report
    print("=" * 80)
    print(f" INTEGRATED SERENITY DOSSIER: {symbol} ")
    print("=" * 80)
    
    if quant_result:
        print(f"PART 1: QUANTITATIVE SENTIMENT SIGNAL")
        print(f"  X Signal Score : {quant_result['score']} / 100 ({quant_result['rating']})")
        print(f"  Ledger Mentions: {quant_result['metrics']['mentions']} times")
        if quant_result['metrics']['mentions'] > 0:
            print(f"  Active Months  : {quant_result['metrics']['active_months']} months")
            print(f"  First Mention  : {quant_result['metrics']['first_mention']}")
            print(f"  Latest Mention : {quant_result['metrics']['last_mention']} ({quant_result['metrics']['days_since_last']} days ago)")
            print(f"  Summary        : {quant_result['summary']}")
            
            print("\n  Top Evidence Tweets:")
            for idx, ev in enumerate(quant_result.get('evidence', [])[:3], 1):
                print(f"    [{idx}] {ev['created_at']} (Engagement: {ev['engagement']}) -> {ev['url']}")
        print("-" * 80)
    
    # 4. Handle Bottleneck Scorecard if provided
    scorecard_path = args.scorecard
    if scorecard_path:
        scorecard_file = Path(scorecard_path)
        if scorecard_file.exists():
            try:
                with open(scorecard_file, 'r', encoding='utf-8') as f:
                    card_data = json.load(f)
                
                # If template contains EXAMPLE/placeholder company, update it
                if card_data.get('ticker') == 'EXAMPLE' or card_data.get('ticker') == symbol:
                    card_data['ticker'] = symbol
                
                bottleneck_res, verdict = score_bottleneck(card_data)
                print(f"PART 2: QUALITATIVE SUPPLY-CHAIN BOTTLENECK SCORECARD")
                print(bottleneck_to_markdown(bottleneck_res))
                if db_path and db_path.exists():
                    save_scorecard_to_db(db_path, symbol, bottleneck_res)
            except Exception as e:
                print(f"Error parsing scorecard JSON: {e}", file=sys.stderr)
        else:
            print(f"Scorecard file '{scorecard_path}' not found.", file=sys.stderr)
    else:
        print(f"PART 2: QUALITATIVE SUPPLY-CHAIN BOTTLENECK SCORECARD")
        print(f"  [Notice] No scorecard JSON provided.")
        print(f"  To run a bottleneck analysis, please supply a scorecard JSON file using --scorecard.")
        if quant_result:
            print(f"  Tip: You can export a pre-filled template for {symbol} by running:")
            print(f"       python scripts/integrated_scorer.py {symbol} --export-template > data/{symbol.lower()}_scorecard_template.json")
    print("=" * 80)


if __name__ == '__main__':
    main()
