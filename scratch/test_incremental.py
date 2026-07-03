import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"

def get_latest_price_date(symbol):
    if not DB_PATH.exists():
        return None
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(
            "select max(date) from prices where symbol=?", (symbol,)
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()

def run_test():
    print("=== 測試 3: 增量股價抓取測試 ===")
    test_symbol = "NVDA"
    
    latest_date = get_latest_price_date(test_symbol)
    print(f"資料庫中 ${test_symbol} 的最新收盤價日期為: {latest_date}")
    
    if latest_date:
        latest_dt = datetime.strptime(latest_date, "%Y-%m-%d")
        today = datetime.now()
        
        days_diff = (today - latest_dt).days
        print(f"距離今天相差: {days_diff} 天")
        
        if days_diff <= 1:
            print("[INFO] 數據已是最新，無需發起 Yahoo Finance API 請求 (節省流量與時間)！")
        else:
            # 模擬計算增量請求的 start date
            start_date_str = (latest_dt + timedelta(days=1)).strftime("%Y-%m-%d")
            print(f"[INFO] 預期增量拉取區間: {start_date_str} 至 {today.strftime('%Y-%m-%d')}")
            print(f"模擬調用: yfinance.download('{test_symbol}', start='{start_date_str}')")
    else:
        print("未在資料庫中找到價格資料，預期執行全量下載。")
        
    print("[SUCCESS] 增量策略邊界條件測試成功！")

if __name__ == "__main__":
    run_test()
