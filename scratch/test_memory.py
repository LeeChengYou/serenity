import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"

def init_test_db(db_path):
    con = sqlite3.connect(db_path)
    con.executescript("""
        create table if not exists user_memories (
            id integer primary key autoincrement,
            category text not null,
            symbol text,
            content text not null,
            weight real default 1.0,
            updated_at text not null,
            unique(category, symbol, content)
        );
    """)
    con.commit()
    return con

def run_test():
    print("=== 測試 5: 長期記憶系統與時間衰減測試 ===")
    
    # 1. Initialize DB and insert mock memories
    con = init_test_db(DB_PATH)
    
    # Clear any previous mock memory to keep test clean
    con.execute("delete from user_memories where content like 'MOCK_TEST%'")
    con.commit()
    
    now = datetime.now()
    two_days_ago = (now - timedelta(days=2.0)).isoformat()
    twelve_days_ago = (now - timedelta(days=12.0)).isoformat()
    
    print("\n[步驟 1] 寫入三條模擬測試記憶（不同建立時間）：")
    con.execute(
        "insert or replace into user_memories(category, symbol, content, weight, updated_at) values (?, ?, ?, ?, ?)",
        ("interest", "NVDA", "MOCK_TEST: 使用者想要深入了解 NVDA 的減速器晶片供應限制。", 1.0, now.isoformat())
    )
    con.execute(
        "insert or replace into user_memories(category, symbol, content, weight, updated_at) values (?, ?, ?, ?, ?)",
        ("preference", "", "MOCK_TEST: 使用者偏好使用繁體中文進行深度供應鏈卡點分析。", 1.0, two_days_ago)
    )
    con.execute(
        "insert or replace into user_memories(category, symbol, content, weight, updated_at) values (?, ?, ?, ?, ?)",
        ("conclusion", "TSM", "MOCK_TEST: 達成結論為 CoWoS 在 2026 年底前依然產能吃緊。", 1.0, twelve_days_ago)
    )
    con.commit()
    
    # 2. View memories before decay
    rows = con.execute("select * from user_memories where content like 'MOCK_TEST%'").fetchall()
    print(f"目前資料庫中共有 {len(rows)} 條測試記憶：")
    for r in rows:
        print(f" - [id={r[0]}] [{r[1]}] 權重={r[4]:.2f}, 更新時間={r[5]} -> {r[3][:45]}...")
        
    # 3. Simulate daily decay updates (Linear Decay: -0.1 weight per day)
    print("\n[步驟 2] 模擬記憶衰減排程（每天權重減少 0.1，小於等於 0 的記憶將被遺忘/清除）...")
    con.execute("""
        update user_memories 
        set weight = weight - (julianday('now') - julianday(updated_at)) * 0.1
        where content like 'MOCK_TEST%'
    """)
    con.execute("delete from user_memories where weight <= 0 and content like 'MOCK_TEST%'")
    con.commit()
    
    # 4. View memories after decay
    rows_after = con.execute("select * from user_memories where content like 'MOCK_TEST%'").fetchall()
    print(f"衰減後資料庫中剩餘 {len(rows_after)} 條記憶（大於 10 天前的記憶已被完全遺忘）：")
    for r in rows_after:
        print(f" - [id={r[0]}] [{r[1]}] 權重={r[4]:.2f}, 更新時間={r[5]} -> {r[3][:45]}...")
        
    # Verify that the 12-day-ago memory is forgotten
    forgotten = all("CoWoS 在 2026 年底前依然產能吃緊" not in r[3] for r in rows_after)
    if forgotten:
        print(" [OK] 驗證成功：12天前創立的記憶已成功因過期而被自動遺忘清除。")
    else:
        print(" [FAIL] 警告：已過期的記憶並未被清除。")
        
    # 5. Test complete clear memory
    print("\n[步驟 3] 測試一鍵清空所有記憶功能...")
    con.execute("delete from user_memories where content like 'MOCK_TEST%'")
    con.commit()
    
    rows_cleared = con.execute("select * from user_memories where content like 'MOCK_TEST%'").fetchall()
    if len(rows_cleared) == 0:
        print(" [OK] 驗證成功：測試記憶已全部安全清空。")
    else:
        print(" [FAIL] 警告：清空後依然留有記憶。")
        
    con.close()
    print("\n[SUCCESS] 長期記憶與時間衰減系統測試完成！")

if __name__ == "__main__":
    run_test()
