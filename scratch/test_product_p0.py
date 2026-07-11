# -*- coding: utf-8 -*-
"""PM 審查 P0/P1 修正驗收測試（由主對話定義）

執行：PYTHONIOENCODING=utf-8 python scratch/test_product_p0.py
通過標準：0 failed、exit 0。零 Gemini 呼叫、零真實網路抓取。

契約重點：
- watchlist 資料表（symbol PK, added_at）；symbol_list = mentions(≥min) ∪ watchlist（排除基準）
- 空 DB 初始化時自動種入預設觀察清單（DEFAULT_WATCHLIST ≥ 15 檔）；非空 DB 不種
- config.DB_PATH 支援環境變數 SERENITY_DB_PATH 覆寫（與 scorer skill 同名）
- GET /api/watchlist、POST /api/watchlist {"add"|"remove": sym}（格式驗證、大寫化、冪等）
- /api/summary 加 signal_distribution {date, counts}；watchlist-only symbol 也出現在 symbols
- /api/hitrate 與 /api/arena/leaderboard 加 sample_days（signal_history / agent_nav_daily 的 DISTINCT date 數）
- scripts/batch_scorecards.py --dry-run --limit N：列出缺記分卡或 >14 天的 symbol 計畫，不打 Gemini
- serenity/services/*.py 不得再直讀 os.environ.get("GEMINI...（統一走 config.get_setting）
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8795
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
REAL_DB = ROOT / "data" / "serenity.sqlite"
TEST_SYM = "ZZZZ"  # 不存在的代號，僅測 watchlist CRUD，結束後移除

results = []


def record(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok))
    print(("PASS" if ok else "FAIL"), name, ("" if ok else f"| {detail}"))


def http(method, path, payload=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def wait_port(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def main():
    # ── 靜態檢查 ────────────────────────────────────────────────────────────
    bad = []
    for f in (ROOT / "serenity" / "services").glob("*.py"):
        if 'os.environ.get("GEMINI' in f.read_text(encoding="utf-8"):
            bad.append(f.name)
    record("S1 services 不再直讀 GEMINI 環境變數（統一 get_setting）", not bad, str(bad))

    # ── 空 DB 種子 ──────────────────────────────────────────────────────────
    tmp = Path(tempfile.mkdtemp(prefix="seed_test_"))
    seed_db = tmp / "fresh.sqlite"
    r = subprocess.run(
        [sys.executable, "-c",
         "import sqlite3, json, os\n"
         "from serenity.db import db\n"
         "con = db()\n"
         "n = con.execute('SELECT COUNT(*) FROM watchlist').fetchone()[0]\n"
         "print('SEEDED', n)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**ENV, "SERENITY_DB_PATH": str(seed_db), "SERENITY_NO_DOTENV": "1"},
        cwd=str(ROOT), timeout=120)
    seeded = 0
    for line in r.stdout.splitlines():
        if line.startswith("SEEDED"):
            seeded = int(line.split()[1])
    record("S2 空 DB 自動種入預設觀察清單（≥15 檔）", r.returncode == 0 and seeded >= 15,
           f"rc={r.returncode} seeded={seeded} err={r.stderr[-300:]}")

    con = sqlite3.connect(str(REAL_DB))
    has_wl = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone()
    dev_wl_before = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] if has_wl else None
    con.close()

    # ── 啟動 server（真 DB，唯讀＋watchlist CRUD 自清理）────────────────────
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "server.py"), "--port", str(PORT)],
        cwd=str(ROOT), env=ENV,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port():
            print("FAIL server 未啟動")
            return 1

        # T1 watchlist GET
        st, body = http("GET", "/api/watchlist")
        d = json.loads(body)
        record("T1 GET /api/watchlist 200 且含 symbols 清單",
               st == 200 and isinstance(d.get("symbols"), list), body[:200])

        # T2 add（小寫輸入 → 大寫存）
        st, body = http("POST", "/api/watchlist", {"add": TEST_SYM.lower()})
        st2, body2 = http("GET", "/api/watchlist")
        syms = [s["symbol"] for s in json.loads(body2)["symbols"]]
        record("T2 add 成功且大寫化", st == 200 and TEST_SYM in syms,
               f"st={st} syms={syms[:10]}")

        # T3 非法格式擋 400
        st, body = http("POST", "/api/watchlist", {"add": "bad symbol!"})
        record("T3 非法代號回 400", st == 400, f"st={st} {body[:200]}")

        # T4 summary 聯集：watchlist-only symbol 出現在 symbols
        st, body = http("GET", "/api/summary")
        sd = json.loads(body)
        sum_syms = [s.get("symbol") for s in sd.get("symbols", [])]
        record("T4 summary.symbols 含 watchlist-only 代號", TEST_SYM in sum_syms,
               f"{TEST_SYM} not in summary ({len(sum_syms)} symbols)")

        # T5 signal_distribution
        dist = sd.get("signal_distribution") or {}
        con = sqlite3.connect(str(REAL_DB))
        row = con.execute(
            "SELECT date, COUNT(*) FROM signal_history WHERE date=(SELECT MAX(date) FROM signal_history)").fetchone()
        con.close()
        counts_sum = sum((dist.get("counts") or {}).values())
        record("T5 summary.signal_distribution 與 DB 一致",
               dist.get("date") == row[0] and counts_sum == row[1],
               f"dist={dist} db={row}")

        # T6 hitrate sample_days
        st, body = http("GET", "/api/hitrate")
        hd = json.loads(body)
        con = sqlite3.connect(str(REAL_DB))
        days = con.execute("SELECT COUNT(DISTINCT date) FROM signal_history").fetchone()[0]
        con.close()
        record("T6 hitrate.sample_days 正確", hd.get("sample_days") == days,
               f"api={hd.get('sample_days')} db={days}")

        # T7 arena leaderboard sample_days
        st, body = http("GET", "/api/arena/leaderboard")
        ad = json.loads(body)
        con = sqlite3.connect(str(REAL_DB))
        adays = con.execute("SELECT COUNT(DISTINCT date) FROM agent_nav_daily").fetchone()[0]
        con.close()
        record("T7 leaderboard.sample_days 正確", ad.get("sample_days") == adays,
               f"api={ad.get('sample_days')} db={adays}")

        # T8 remove + 冪等
        st, _ = http("POST", "/api/watchlist", {"remove": TEST_SYM})
        st2, _ = http("POST", "/api/watchlist", {"remove": TEST_SYM})
        st3, body3 = http("GET", "/api/watchlist")
        syms = [s["symbol"] for s in json.loads(body3)["symbols"]]
        record("T8 remove 成功且冪等", st == 200 and st2 == 200 and TEST_SYM not in syms,
               f"st={st},{st2} syms={syms[:10]}")

        # T9 前端 DOM 掛勾
        st, body = http("GET", "/index.html")
        needed = ["signal-distribution", "watchlist"]
        missing = [k for k in needed if k not in body]
        record("T9 index.html 含 signal-distribution 與 watchlist UI", not missing,
               f"缺 {missing}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # T10 dev DB watchlist 未被測試污染（若表原本存在則數量不變）
    con = sqlite3.connect(str(REAL_DB))
    dev_wl_after = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    con.close()
    record("T10 真 DB watchlist 未殘留測試代號",
           dev_wl_before is None or dev_wl_after == dev_wl_before,
           f"before={dev_wl_before} after={dev_wl_after}")

    # T11 batch_scorecards --dry-run（零 Gemini）
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "batch_scorecards.py"),
         "--dry-run", "--limit", "5"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=ENV, cwd=str(ROOT), timeout=120)
    plan_lines = [l for l in r.stdout.splitlines() if l.startswith("PLAN ")]
    record("T11a dry-run exit 0 且列出計畫（PLAN 行）", r.returncode == 0 and len(plan_lines) >= 1,
           f"rc={r.returncode} out={r.stdout[:300]} err={r.stderr[-200:]}")
    record("T11b --limit 5 生效", len(plan_lines) <= 5, f"{len(plan_lines)} 行")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
