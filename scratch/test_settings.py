# -*- coding: utf-8 -*-
"""階段二（設定系統 + 設定 API）驗收測試（由主對話定義，實作必須通過全部案例）

執行：PYTHONIOENCODING=utf-8 python scratch/test_settings.py
通過標準：0 failed、exit 0。零真實 Gemini 呼叫（假 key 只寫進暫存 config，不打網路）。

契約重點：
- SERENITY_HOME 環境變數 → 設定目錄（config.json 所在）
- SERENITY_NO_DOTENV=1 → 跳過 .env 載入（測試隔離用）
- 設定解析順序：環境變數 > SERENITY_HOME/config.json > 預設值
- GET /api/settings 回 {"has_key", "keys":[{slot,set,masked}x4], "models":{...}, "config_path"}
- POST /api/settings 部分更新；空字串=清除；寫入 config.json 後 KeyManager 熱重載
- 完整 key 永不出現在任何 API 回應
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
PORT = 8790
FAKE_KEY = "AIzaFAKE_TEST_KEY_00001234"  # 尾碼 1234，非真實金鑰

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
    home = Path(tempfile.mkdtemp(prefix="serenity_home_test_"))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GEMINI_API_KEY")}
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "SERENITY_HOME": str(home),
        "SERENITY_NO_DOTENV": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "server.py"), "--port", str(PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port():
            print("FAIL server 未在 30 秒內開始監聽")
            return 1

        # T1 無 key 初始狀態
        st, body = http("GET", "/api/settings")
        d = json.loads(body)
        record("T1a GET /api/settings 200", st == 200, f"status={st}")
        record("T1b has_key=false（無 .env、無 config）", d.get("has_key") is False, body[:300])
        record("T1c keys 為 4 個 slot 且皆未設定", isinstance(d.get("keys"), list)
               and len(d["keys"]) == 4 and all(not k.get("set") for k in d["keys"]),
               str(d.get("keys"))[:300])

        # T2 寫入假 key
        st, body = http("POST", "/api/settings", {"gemini_api_key": FAKE_KEY})
        record("T2a POST 設定 key 回 200", st == 200, f"status={st} {body[:200]}")
        cfg_file = home / "config.json"
        record("T2b config.json 寫入 SERENITY_HOME", cfg_file.exists(), str(cfg_file))
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
        record("T2c config.json 內含完整 key", cfg.get("gemini_api_key") == FAKE_KEY,
               str(cfg)[:200])
        record("T2d POST 回應不含完整 key", FAKE_KEY not in body, body[:300])

        # T3 遮罩顯示
        st, body = http("GET", "/api/settings")
        d = json.loads(body)
        record("T3a has_key=true", d.get("has_key") is True, body[:300])
        record("T3b 回應含尾碼 1234 但不含完整 key",
               "1234" in body and FAKE_KEY not in body, body[:300])
        record("T3c slot1 set=true", d["keys"][0].get("set") is True, str(d["keys"][0]))

        # T4 KeyManager 熱重載（不重啟 server）
        st, body = http("GET", "/api/keypool")
        record("T4a keypool 看得到尾碼 ...1234", "...1234" in body, body[:300])
        record("T4b keypool 不洩漏完整 key", FAKE_KEY not in body, body[:300])

        # T5 其他端點不洩漏
        st, body = http("GET", "/api/config")
        record("T5 /api/config 不洩漏完整 key", FAKE_KEY not in body, body[:300])

        # T6 冪等重送
        st, body = http("POST", "/api/settings", {"gemini_api_key": FAKE_KEY})
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        record("T6 重複 POST 冪等", st == 200 and cfg.get("gemini_api_key") == FAKE_KEY,
               f"status={st}")

        # T7 部分更新模型不清掉 key
        st, body = http("POST", "/api/settings", {"gemini_model": "gemini-2.5-pro"})
        st2, body2 = http("GET", "/api/settings")
        d2 = json.loads(body2)
        record("T7a 模型更新生效", d2.get("models", {}).get("gemini_model") == "gemini-2.5-pro",
               body2[:300])
        record("T7b key 未被部分更新清掉", d2.get("has_key") is True, body2[:300])

        # T8 清除 key
        st, body = http("POST", "/api/settings", {"gemini_api_key": ""})
        st2, body2 = http("GET", "/api/settings")
        d2 = json.loads(body2)
        record("T8 空字串清除 key → has_key=false", d2.get("has_key") is False, body2[:300])

        # T9 /api/settings/test 空 key 立即失敗（不打網路）
        st, body = http("POST", "/api/settings/test", {"key": ""})
        d = json.loads(body)
        record("T9 test 端點空 key 回 ok=false", d.get("ok") is False, body[:200])

        failed = [n for n, ok in results if not ok]
        print(f"\n{len(results) - len(failed)}/{len(results)} passed, {len(failed)} failed")
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        import shutil
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
