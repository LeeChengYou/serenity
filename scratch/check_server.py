import urllib.request
import json

month = "2026-07"

def fetch(url):
    try:
        r = urllib.request.urlopen(url)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

print("=== /api/arena/leaderboard ===")
data = fetch(f"http://127.0.0.1:8787/api/arena/leaderboard?month={month}")
print(json.dumps(data, indent=2, ensure_ascii=False)[:1000])

print()
print("=== /api/arena/nav ===")
data2 = fetch(f"http://127.0.0.1:8787/api/arena/nav?month={month}")
print(json.dumps(data2, indent=2, ensure_ascii=False)[:1000])

print()
print("=== /api/arena/trades (semis-dip) ===")
data3 = fetch(f"http://127.0.0.1:8787/api/arena/trades?agent=semis-dip&month={month}")
print(json.dumps(data3, indent=2, ensure_ascii=False)[:800])
