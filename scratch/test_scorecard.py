import sqlite3
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"

def run_test():
    print("=== 測試 1: 投研指標與卡點評分系統測試 ===")
    if not DB_PATH.exists():
        print(f"❌ 錯誤: 找不到資料庫 {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        # 測試：查詢是否有 symbol mentions，並模擬生成卡點評級
        rows = con.execute("""
            select symbol, count(*) count from mentions 
            group by symbol order by count desc limit 5
        """).fetchall()
        
        print("最新提及熱度前 5 名:")
        for r in rows:
            symbol = r['symbol']
            count = r['count']
            
            # 模擬計算評級指標 (物理限制、稀缺程度、定價權)
            # 在真實系統中，這些將由 integrated_scorer 寫入資料庫
            physical_barrier = min(10, count // 2)
            scarcity_score = 7 if count > 10 else 4
            pricing_power = 8 if symbol in ['NVDA', 'TSM'] else 5
            
            total_score = (physical_barrier + scarcity_score + pricing_power) / 3
            grade = 'A' if total_score >= 7 else ('B' if total_score >= 5 else 'C')
            
            print(f" - ${symbol}: 提及次數 {count} | 模擬卡點總分: {total_score:.1f}/10 | 評級: {grade}")
            
        print("[SUCCESS] 評分指標載入與模擬計算測試成功！")
    except Exception as e:
        print(f"[FAIL] 測試失敗: {e}")
    finally:
        con.close()

if __name__ == "__main__":
    run_test()
