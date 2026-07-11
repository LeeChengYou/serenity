# -*- coding: utf-8 -*-
"""資料時效自檢功能（/api/health + 自動補抓）驗收測試（由主對話定義）

執行：PYTHONIOENCODING=utf-8 python scratch/test_health.py
通過標準：0 failed、exit 0。
安全原則：只做唯讀檢查與 dry_run 計畫，不真抓網路資料、不打 Gemini、不寫真 DB。

契約重點：
- GET /api/health → daily_check 十項檢查的 in-process 版（單一事實來源：
  importlib 載入 scripts/daily_check.py 的 run_all_checks，不得複製門檻邏輯）
- POST /api/admin/refresh {"dry_run": true} → {"dry_run": true, "domains": [過期∩安全], "manual": [過期∖安全]}
- GET /api/admin/refresh/status → {"running": bool, "steps": [...]}
- serenity.background.plan_auto_refresh() → 目前該自動補抓的安全域清單（背景每小時用）
- 安全域 = prices/benchmarks/signal_history/news/stocktwits/fundamentals/estimates
  （tweets/expert_views/arena_nav 永不自動跑：需 cookies/Playwright/Gemini）
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8793
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

EXPECTED_NAMES = {
    "prices", "benchmarks", "signal_history", "news", "stocktwits",
    "tweets", "fundamentals", "estimates", "expert_views", "arena_nav",
}
SAFE_DOMAINS = {
    "prices", "benchmarks", "signal_history", "news", "stocktwits",
    "fundamentals", "estimates",
}

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
    # 對照組：daily_check CLI 的結果（唯讀）
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "daily_check.py"), "check", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=ENV, cwd=str(ROOT), timeout=300)
    cli = json.loads(r.stdout)
    cli_status = {c["name"]: c["status"] for c in cli["checks"]}

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "server.py"), "--port", str(PORT)],
        cwd=str(ROOT), env=ENV,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port():
            print("FAIL server 未在 30 秒內開始監聽")
            return 1

        # T1 /api/health 基本形狀
        st, body = http("GET", "/api/health")
        d = json.loads(body)
        record("T1a /api/health 200", st == 200, f"status={st}")
        record("T1b 含 ok/as_of/checks/checked_at",
               all(k in d for k in ("ok", "as_of", "checks", "checked_at")), body[:300])
        names = {c["name"] for c in d.get("checks", [])}
        record("T1c 十項檢查名齊全", names == EXPECTED_NAMES,
               f"缺:{EXPECTED_NAMES - names} 多:{names - EXPECTED_NAMES}")

        # T2 與 daily_check CLI 結果一致（單一事實來源驗證）
        api_status = {c["name"]: c["status"] for c in d.get("checks", [])}
        record("T2 health 與 daily_check --json 狀態一致", api_status == cli_status,
               f"api={api_status} cli={cli_status}")

        # T3 refresh dry_run：計畫 = 過期∩安全域；manual = 過期∖安全域
        stale = {n for n, s in api_status.items() if s != "ok"}
        st, body = http("POST", "/api/admin/refresh", {"dry_run": True})
        rd = json.loads(body)
        record("T3a dry_run 200 且標記 dry_run", st == 200 and rd.get("dry_run") is True,
               body[:300])
        record("T3b domains = 過期∩安全域", set(rd.get("domains", [])) == (stale & SAFE_DOMAINS),
               f"回報={rd.get('domains')} 期望={sorted(stale & SAFE_DOMAINS)}")
        record("T3c manual = 過期∖安全域", set(rd.get("manual", [])) == (stale - SAFE_DOMAINS),
               f"回報={rd.get('manual')} 期望={sorted(stale - SAFE_DOMAINS)}")

        # T4 refresh status 初始狀態
        st, body = http("GET", "/api/admin/refresh/status")
        sd = json.loads(body)
        record("T4 status 200 且 running=false",
               st == 200 and sd.get("running") is False and "steps" in sd, body[:300])

        # T5 dry_run 不留任何執行痕跡（job_runs 增量=0）
        import sqlite3
        con = sqlite3.connect(str(ROOT / "data" / "serenity.sqlite"))
        n = con.execute(
            "SELECT COUNT(*) FROM job_runs WHERE mode IN ('ui-refresh','auto')").fetchone()[0]
        con.close()
        st, _ = http("POST", "/api/admin/refresh", {"dry_run": True})
        con = sqlite3.connect(str(ROOT / "data" / "serenity.sqlite"))
        n2 = con.execute(
            "SELECT COUNT(*) FROM job_runs WHERE mode IN ('ui-refresh','auto')").fetchone()[0]
        con.close()
        record("T5 dry_run 不寫 job_runs", n2 == n, f"before={n} after={n2}")

        # T6 前端含資料時效徽章
        st, body = http("GET", "/index.html")
        record("T6 index.html 含 health-badge 與 health-panel",
               "health-badge" in body and "health-panel" in body, "DOM id 缺失")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # T7 背景自動補抓的計畫函式（不起 server 也可用；唯讀）
    r = subprocess.run(
        [sys.executable, "-c",
         "import json; from serenity.background import plan_auto_refresh; "
         "print(json.dumps(plan_auto_refresh()))"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=ENV, cwd=str(ROOT), timeout=120)
    try:
        plan = set(json.loads(r.stdout.strip().splitlines()[-1]))
        ok7 = plan <= SAFE_DOMAINS
    except Exception:
        plan, ok7 = None, False
    record("T7 plan_auto_refresh() 可呼叫且只含安全域", r.returncode == 0 and ok7,
           f"rc={r.returncode} out={r.stdout[-200:]} err={r.stderr[-200:]}")

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
