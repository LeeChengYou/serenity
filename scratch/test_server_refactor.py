# -*- coding: utf-8 -*-
"""server.py 模組化重構的行為對照測試（由主對話定義）

用法：
  python scratch/test_server_refactor.py --capture   # 重構前跑：擷取黃金基準 baseline_api.json
  python scratch/test_server_refactor.py             # 重構後跑：逐端點比對 status code 與 JSON 頂層 key 集合

比對原則：資料值每天會變，所以只比對「回應形狀」——HTTP status + JSON 頂層 key 集合
（list 回應則比對第一個元素的 key 集合與 list 型別）。
不打 /api/dossier 與任何 POST（避免真實 Gemini 呼叫）。
通過標準：0 failed、exit 0。
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
BASELINE = ROOT / "scratch" / "baseline_api.json"
PORT = 8788
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

ENDPOINTS = [
    "/api/config", "/api/monitor", "/api/keypool", "/api/regime",
    "/api/hitrate", "/api/changes?days=7", "/api/summary", "/api/feed",
    "/api/symbol/NVDA", "/api/signal/NVDA", "/api/signal/history/NVDA",
    "/api/news/NVDA", "/api/estimates/NVDA", "/api/fundamentals/NVDA",
    "/api/scorecard/NVDA", "/api/scorecard/history/NVDA", "/api/memory",
    "/api/expert-views", "/api/expert-views/NVDA",
    "/api/arena/leaderboard", "/api/arena/nav", "/api/arena/trades",
    "/api/arena/reflections", "/index.html",
]


def shape_of(body: bytes, path: str):
    """回傳可比對的形狀描述。靜態檔只看非空。"""
    if path == "/index.html":
        return {"type": "html", "nonempty": len(body) > 100}
    try:
        data = json.loads(body)
    except Exception:
        return {"type": "unparseable"}
    if isinstance(data, dict):
        return {"type": "dict", "keys": sorted(data.keys())}
    if isinstance(data, list):
        first = sorted(data[0].keys()) if data and isinstance(data[0], dict) else None
        return {"type": "list", "first_item_keys": first}
    return {"type": type(data).__name__}


def fetch(path: str):
    url = f"http://127.0.0.1:{PORT}{path}"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


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
    capture = "--capture" in sys.argv
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "server.py"), "--port", str(PORT)],
        cwd=str(ROOT), env=ENV,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port():
            print("FAIL server 未在 30 秒內開始監聽")
            return 1

        results = {}
        for ep in ENDPOINTS:
            status, body = fetch(ep)
            results[ep] = {"status": status, "shape": shape_of(body, ep)}

        if capture:
            BASELINE.write_text(
                json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"已擷取 {len(results)} 個端點基準 → {BASELINE}")
            return 0

        if not BASELINE.exists():
            print("FAIL 找不到 baseline_api.json，先在重構前跑 --capture")
            return 1
        golden = json.loads(BASELINE.read_text(encoding="utf-8"))
        failed = 0
        for ep in ENDPOINTS:
            g, r = golden.get(ep), results.get(ep)
            if g == r:
                print(f"PASS {ep}")
            else:
                failed += 1
                print(f"FAIL {ep}\n  基準: {g}\n  現況: {r}")
        print(f"\n{len(ENDPOINTS) - failed}/{len(ENDPOINTS)} passed, {failed} failed")
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
