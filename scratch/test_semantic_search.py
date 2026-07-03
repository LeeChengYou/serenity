import sqlite3
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"

def keyword_search(db_path, query_keywords):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    results = []
    try:
        # 尋找內容包含特定主題關鍵字的推文
        # 建立 SQL 模糊匹配語句
        conditions = " OR ".join(["text LIKE ?" for _ in query_keywords])
        params = [f"%{k}%" for k in query_keywords]
        
        sql = f"""
            select m.symbol, m.text, m.mentioned_at 
            from mentions m
            where {conditions}
            order by m.mentioned_at desc limit 5
        """
        
        rows = con.execute(sql, params).fetchall()
        for r in rows:
            results.append(dict(r))
    except Exception as e:
        print(f"查詢錯誤: {e}")
    finally:
        con.close()
    return results

def run_test():
    print("=== 測試 2: 供應鏈語意/關鍵字關聯 RAG 測試 ===")
    if not DB_PATH.exists():
        print(f"[ERROR] 錯誤: 找不到資料庫 {DB_PATH}")
        return

    # 模擬提問語意：使用者想研究「先進封裝」或「散熱卡點」
    test_queries = [
        {"keywords": ["packaging", "cowos", "封装"], "name": "先進封裝主題"},
        {"keywords": ["cooling", "liquid", "散热", "液冷"], "name": "散熱設備主題"}
    ]
    
    for q in test_queries:
        print(f"\n[搜尋主題] {q['name']} (關鍵字: {q['keywords']})")
        matched = keyword_search(DB_PATH, q['keywords'])
        if matched:
            print(f"找到 {len(matched)} 條相關本地討論事實：")
            for idx, item in enumerate(matched, 1):
                clean_text = re.sub(r'\s+', ' ', item['text'])[:80] + "..."
                print(f" {idx}. [${item['symbol']}] [{item['mentioned_at']}] {clean_text}")
        else:
            print(" [WARN] 未在本地資料庫中找到相符的討論推文。")

    print("\n[SUCCESS] 語意關聯搜尋測試完成！")

if __name__ == "__main__":
    run_test()
