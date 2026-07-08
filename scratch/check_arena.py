import sqlite3

c = sqlite3.connect('data/serenity.sqlite')

print('=== agent_nav_daily 最新兩天 ===')
rows = c.execute('SELECT agent_id, date, nav, cash FROM agent_nav_daily ORDER BY date DESC, agent_id LIMIT 18').fetchall()
for r in rows:
    print(r)

print()
print('=== agent_trades 狀態彙總 ===')
rows2 = c.execute('SELECT status, count(*) FROM agent_trades GROUP BY status').fetchall()
for r in rows2:
    print(r)

print()
print('=== 2026-07-07 新增決策 ===')
rows3 = c.execute(
    "SELECT agent_id, decided_date, symbol, side, usd, status, rejected_reason "
    "FROM agent_trades WHERE decided_date='2026-07-07' ORDER BY agent_id"
).fetchall()
print(f'count = {len(rows3)}')
for r in rows3:
    print(r)

print()
print('=== 2026-07-06 pending 單目前狀態 ===')
rows4 = c.execute(
    "SELECT agent_id, decided_date, exec_date, symbol, side, qty, price, status "
    "FROM agent_trades WHERE decided_date='2026-07-06' ORDER BY agent_id"
).fetchall()
for r in rows4:
    print(r)

c.close()
