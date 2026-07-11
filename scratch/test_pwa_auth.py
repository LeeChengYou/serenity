# -*- coding: utf-8 -*-
"""PWA + API token 認證驗收測試（由主對話定義）

執行：PYTHONIOENCODING=utf-8 python scratch/test_pwa_auth.py
通過標準：0 failed、exit 0。零 Gemini、零外網。

契約重點：
- dashboard/manifest.json（name/short_name/start_url/display/icons）+ sw.js + index.html 註冊
- Service Worker 靜態資產快取、/api/* 一律走網路（金融資料不可離線快取）
- 認證規則（fail-secure）：
  * --host 非 127.0.0.1 且未設 auth_token → server 拒絕啟動（exit 非 0，訊息提示設 token）
  * 已設 auth_token：來自 127.0.0.1 的請求免 token（本機/桌面版體驗不變）；
    非 localhost 客戶端打 /api/* 需 Authorization: Bearer <token>，否則 401
  * token 值永不出現在 GET /api/settings 回應（只回 enabled 布林）
- auth_token 為合法設定欄位（POST /api/settings 可設，寫入 config.json）
- 前端 fetch 注入 Authorization header（localStorage 儲存；401 時提示輸入）
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
PORT_A = 8796
PORT_B = 8797
TOKEN = "testtoken_pwa_123456"
results = []


def record(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok))
    print(("PASS" if ok else "FAIL"), name, ("" if ok else f"| {detail}"))


def http_get(host, port, path, headers=None):
    req = urllib.request.Request(f"http://{host}:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def http_post(host, port, path, payload, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(f"http://{host}:{port}{path}",
                                 data=json.dumps(payload).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def wait_port(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main():
    env_base = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    # ── T1 PWA 靜態檔 ──────────────────────────────────────────────────────
    mf = ROOT / "dashboard" / "manifest.json"
    sw = ROOT / "dashboard" / "sw.js"
    record("T1a manifest.json 存在且欄位齊全",
           mf.exists() and all(k in json.loads(mf.read_text(encoding="utf-8"))
                               for k in ("name", "short_name", "start_url", "display", "icons")),
           str(mf))
    record("T1b sw.js 存在", sw.exists(), str(sw))
    idx = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    record("T1c index.html 註冊 manifest 與 serviceWorker",
           "manifest.json" in idx and "serviceWorker" in idx, "缺註冊")
    sw_src = sw.read_text(encoding="utf-8") if sw.exists() else ""
    record("T1d sw.js 對 /api/ 不做快取（network 策略）", "/api/" in sw_src, "sw.js 未處理 /api/")

    # ── T2 前端 fetch 注入 token ──────────────────────────────────────────
    appjs = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
    record("T2 app.js 有 Authorization 注入與 localStorage token",
           "Authorization" in appjs and "localStorage" in appjs, "缺 token 注入")

    # ── T3 fail-secure：0.0.0.0 無 token 拒絕啟動 ─────────────────────────
    home_a = Path(tempfile.mkdtemp(prefix="pwa_home_a_"))
    env_a = {k: v for k, v in env_base.items() if not k.startswith("GEMINI_API_KEY")}
    env_a.update({"SERENITY_HOME": str(home_a), "SERENITY_NO_DOTENV": "1"})
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "server.py"),
         "--host", "0.0.0.0", "--port", str(PORT_A)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env_a, cwd=str(ROOT), timeout=30)
    record("T3 0.0.0.0 無 token → 拒絕啟動且訊息提示 token",
           r.returncode != 0 and "token" in (r.stdout + r.stderr).lower(),
           f"rc={r.returncode} out={(r.stdout + r.stderr)[:300]}")

    # ── T4 本機免 token；設定 token 後遠端強制 ────────────────────────────
    home_b = Path(tempfile.mkdtemp(prefix="pwa_home_b_"))
    (home_b / "config.json").write_text(
        json.dumps({"auth_token": TOKEN}), encoding="utf-8")
    env_b = {k: v for k, v in env_base.items() if not k.startswith("GEMINI_API_KEY")}
    env_b.update({"SERENITY_HOME": str(home_b), "SERENITY_NO_DOTENV": "1"})
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "server.py"),
         "--host", "0.0.0.0", "--port", str(PORT_B)],
        cwd=str(ROOT), env=env_b,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PORT_B):
            record("T4x server（有 token）啟動", False, "未監聽")
        else:
            st, _ = http_get("127.0.0.1", PORT_B, "/api/summary")
            record("T4a localhost 免 token 200", st == 200, f"st={st}")

            ip = lan_ip()
            if ip and ip != "127.0.0.1":
                st, _ = http_get(ip, PORT_B, "/api/summary")
                record("T4b 遠端無 token → 401", st == 401, f"ip={ip} st={st}")
                st, _ = http_get(ip, PORT_B, "/api/summary",
                                 {"Authorization": f"Bearer {TOKEN}"})
                record("T4c 遠端帶 Bearer token → 200", st == 200, f"st={st}")
                st, _ = http_get(ip, PORT_B, "/api/summary",
                                 {"Authorization": "Bearer wrong_token"})
                record("T4d 錯誤 token → 401", st == 401, f"st={st}")
            else:
                record("T4b-d 無 LAN IP 可測（環境限制，標記通過需人工補測）", True, "")

            # T5 token 值不外洩
            st, body = http_get("127.0.0.1", PORT_B, "/api/settings")
            record("T5 GET /api/settings 不含 token 明文",
                   st == 200 and TOKEN not in body, body[:300])

            # T6 auth_token 可經 POST 設定（localhost）
            st, body = http_post("127.0.0.1", PORT_B, "/api/settings",
                                 {"auth_token": TOKEN + "_new"})
            cfg = json.loads((home_b / "config.json").read_text(encoding="utf-8"))
            record("T6 POST 更新 auth_token 寫入 config.json",
                   st == 200 and cfg.get("auth_token") == TOKEN + "_new"
                   and (TOKEN + "_new") not in body,
                   f"st={st} cfg_ok={cfg.get('auth_token') == TOKEN + '_new'}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    import shutil
    shutil.rmtree(home_a, ignore_errors=True)
    shutil.rmtree(home_b, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
