#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Serenity Signal Catch-up Script
自動補抓漏掉的 X 推文、股價、新聞，並按時間軸順序自動補跑 AI 經理人競技場每日流程。
"""

import os
import sys
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "serenity.sqlite"

def run_cmd(cmd, check=True):
    print(f"\n[catchup] 執行命令: {' '.join(cmd)}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        res = subprocess.run(cmd, cwd=str(ROOT), env=env, check=check, capture_output=True, text=True)
        if res.stdout:
            print(res.stdout.strip())
        if res.stderr:
            print(res.stderr.strip(), file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[catchup] 錯誤: 命令執行失敗 (exit code {e.returncode})", file=sys.stderr)
        if e.stdout:
            print(e.stdout.strip())
        if e.stderr:
            print(e.stderr.strip(), file=sys.stderr)
        if check:
            raise e
        return False

def main():
    print("=" * 60)
    print(f"Serenity 自動回溯補課管線啟動時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ---------------------------------------------------------
    # 1. 抓取最新數據
    # ---------------------------------------------------------
    print("\n>>> 步驟 1: 正在獲取最新市場與社群數據...")
    
    # 1.1 刷新 X cookie (非強制，失敗仍繼續)
    run_cmd([sys.executable, "scripts/crawler.py", "refresh-cookies"], check=False)
    
    # 1.2 抓取 X 貼文與提及
    run_cmd([sys.executable, "scripts/ingest.py", "fetch-x", "--max-pages", "10"], check=False)
    
    # 1.3 更新價格 (此處更新後，新提及個股的價格也會補齊)
    run_cmd([sys.executable, "scripts/ingest.py", "prices"], check=False)
    
    # 1.4 更新基準指數價格
    run_cmd([sys.executable, "scripts/ingest.py", "benchmarks"], check=False)
    
    # 1.5 更新新聞
    run_cmd([sys.executable, "scripts/ingest.py", "news"], check=False)
    
    # 1.6 更新 Stocktwits (常規 403 略過)
    run_cmd([sys.executable, "scripts/ingest.py", "stocktwits"], check=False)

    # ---------------------------------------------------------
    # 2. 分析斷檔並補跑 Arena
    # ---------------------------------------------------------
    print("\n>>> 步驟 2: 分析經理人競技場（Arena）資料斷檔...")
    if not DB_PATH.exists():
        print(f"[錯誤] 未找到資料庫文件: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
        
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # 2.1 獲取最新的 NAV 日期
    cursor.execute("SELECT max(date) FROM agent_nav_daily")
    row = cursor.fetchone()
    last_nav_date = row[0] if row and row[0] else None
    
    # 2.2 獲取最新已入庫的價格日期
    cursor.execute("SELECT max(date) FROM prices")
    row_price = cursor.fetchone()
    latest_price_date = row_price[0] if row_price and row_price[0] else None
    
    if not last_nav_date:
        print("[catchup] 注意：未找到任何 NAV 歷史紀錄，將由 prices 表的最早日期開始補跑。")
        # 尋找 prices 中最早的日期
        cursor.execute("SELECT min(date) FROM prices")
        last_nav_date = cursor.fetchone()[0]
        # 回退一天以便包含該最早日期
        if last_nav_date:
            cursor.execute("SELECT date FROM (SELECT DISTINCT date FROM prices ORDER BY date) LIMIT 1")
            
    print(f"[catchup] 競技場最新淨值日期 (NAV Last Date): {last_nav_date}")
    print(f"[catchup] 市場最新價格日期 (Price Last Date): {latest_price_date}")
    
    if not last_nav_date or not latest_price_date:
        print("[catchup] 無法確定日期區間，跳過 Arena 回溯。")
        conn.close()
    else:
        # 2.3 找出所有需要補跑的交易日
        cursor.execute(
            "SELECT DISTINCT date FROM prices WHERE date > ? AND date <= ? ORDER BY date",
            (last_nav_date, latest_price_date)
        )
        missing_dates = [r[0] for r in cursor.fetchall()]
        conn.close()
        
        if not missing_dates:
            print("[catchup] 競技場已是最新狀態，無需補跑每日流程。")
        else:
            print(f"[catchup] 偵測到 {len(missing_dates)} 個待補跑交易日: {missing_dates}")
            for m_date in missing_dates:
                print("\n" + "=" * 40)
                print(f" 補跑交易日: {m_date}")
                print("=" * 40)
                # 執行每日 Arena 決策與淨值紀錄
                run_cmd([sys.executable, "scripts/agent_arena.py", "daily", "--as-of", m_date])

    # ---------------------------------------------------------
    # 3. 執行今日訊號快照
    # ---------------------------------------------------------
    print("\n>>> 步驟 3: 執行今日訊號快照 (Snapshot)...")
    run_cmd([sys.executable, "scripts/server.py", "--snapshot-once"])

    print("\n" + "=" * 60)
    print("[catchup] 恭喜！Serenity 自動回溯補課管線執行完畢。")
    print("=" * 60)

if __name__ == "__main__":
    main()
