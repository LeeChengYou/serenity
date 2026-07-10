# -*- coding: utf-8 -*-
"""階段三（桌面殼 + in-process 管線 + bootstrap）可自動化部分的驗收測試

執行：PYTHONIOENCODING=utf-8 python scratch/test_desktop.py
通過標準：0 failed、exit 0。零真實網路抓取（bootstrap 只測 dry_run 與 status）。
PyInstaller 打包與真實開窗屬人工驗收（見規格 §3.2），不在本檔範圍。

契約重點：
- serenity/background.py 不得用 subprocess/sys.executable 跑 ingest（打包後會自我啟動）
- serenity/desktop.py 存在 main()；未裝 pywebview 時 import 不炸、執行時給明確訊息
- `python -m serenity.desktop --smoke`：起 server、驗證 HTTP 200、不開窗直接退出（headless 可測）
- POST /api/admin/bootstrap {"dry_run": true} → 回計畫步驟不執行
- GET /api/admin/bootstrap/status → {"running": bool, "steps": [...]}
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8792
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
        with urllib.request.urlopen(req, timeout=60) as resp:
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
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    # T0 語法
    r = subprocess.run([sys.executable, "-m", "py_compile",
                        str(ROOT / "serenity" / "desktop.py"),
                        str(ROOT / "serenity" / "background.py")],
                       capture_output=True, text=True, env=env)
    record("T0 py_compile desktop/background", r.returncode == 0, r.stderr[-300:])

    # T1 background.py 禁用 subprocess 跑 ingest（in-process 規則）
    src = (ROOT / "serenity" / "background.py").read_text(encoding="utf-8")
    record("T1 background.py 無 sys.executable / subprocess",
           "sys.executable" not in src and "subprocess" not in src,
           "打包後 subprocess 會自我啟動")

    # T2 desktop.py 可 import、有 main()（不需 pywebview 已安裝）
    r = subprocess.run(
        [sys.executable, "-c",
         "import serenity.desktop as d; print(callable(d.main))"],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    record("T2 desktop 模組可 import 且有 main()",
           r.returncode == 0 and "True" in r.stdout,
           f"rc={r.returncode} {r.stderr[-300:]}")

    # T3 --smoke 模式：起 server → 自驗 200 → 退出（不開窗）
    r = subprocess.run(
        [sys.executable, "-m", "serenity.desktop", "--smoke", "--port", str(PORT)],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    record("T3 --smoke 自檢通過並正常退出（exit 0）", r.returncode == 0,
           f"rc={r.returncode} out={r.stdout[-200:]} err={r.stderr[-300:]}")

    # T4 bootstrap API（起一般 server 測）
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "server.py"), "--port", str(PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port():
            record("T4x server 啟動", False, "30 秒未監聽")
        else:
            st, body = http("GET", "/api/admin/bootstrap/status")
            d = json.loads(body)
            record("T4a status 端點 200 且含 running/steps",
                   st == 200 and "running" in d and "steps" in d, body[:300])
            record("T4b 初始 running=false", d.get("running") is False, body[:300])

            st, body = http("POST", "/api/admin/bootstrap", {"dry_run": True})
            d = json.loads(body)
            record("T5a dry_run 回 200 且含計畫步驟", st == 200
                   and isinstance(d.get("steps"), list) and len(d["steps"]) >= 3,
                   body[:300])
            joined = " ".join(str(s) for s in d.get("steps", []))
            record("T5b 計畫含 prices/benchmarks/news",
                   all(k in joined for k in ("prices", "benchmarks", "news")), joined[:300])
            st2, body2 = http("GET", "/api/admin/bootstrap/status")
            d2 = json.loads(body2)
            record("T5c dry_run 不真的啟動（running 仍 false）",
                   d2.get("running") is False, body2[:300])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
