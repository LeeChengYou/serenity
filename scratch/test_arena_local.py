# -*- coding: utf-8 -*-
"""
(b-2) 本地 LLM 接管競技場日常決策 — 驗收測試
規格：docs/REQUIREMENTS_AI_MARKET.md b2-驗收 1~6

執行：PYTHONIOENCODING=utf-8 python scratch/test_arena_local.py

原則：
  - 假 HTTP server 模擬 Ollama（比照 test_local_llm.py）
  - 不打真 Ollama、不打真 Gemini
  - DB 只讀 tempfile 副本，絕不寫入正式 DB
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SERENITY_NO_DOTENV", "1")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    mark = "  OK " if ok else "! FAIL"
    print(f"{mark}  {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    return ok


def finish() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print()
    print("=" * 70)
    print(f"Arena Local LLM Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# 假 Ollama HTTP server（比照 test_local_llm.py）
# ---------------------------------------------------------------------------

def _make_handler(response_body: bytes, status: int = 200):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response_body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            _ = self.rfile.read(length)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response_body)

    return _Handler


def _start_server(handler_class):
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def _openai_response(content: str) -> bytes:
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return json.dumps(body).encode("utf-8")


# ---------------------------------------------------------------------------
# 合法 decide JSON
# ---------------------------------------------------------------------------

_VALID_DECIDE = json.dumps({
    "actions": [{"side": "BUY", "symbol": "NVDA", "usd": 500, "reason": "測試"}],
    "watch": ["AAPL"],
    "memory_note": "測試備忘",
})

_VALID_REFLECT = json.dumps({
    "public_letter": "公開信",
    "reflection_md": "反思",
    "strategy_md": "策略",
})


def main():
    import agent_arena as arena

    # ── b2-驗收 1a：decide 假 server 回合法 JSON → 解析出 actions ────────────
    print("\n[b2-1a] decide: 假 server 回合法 JSON")
    ok_server, ok_port = _start_server(
        _make_handler(_openai_response(_VALID_DECIDE))
    )
    os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{ok_port}"
    os.environ["LOCAL_LLM_MODEL"] = "qwen3:14b"
    try:
        backend = arena.LocalBackend()
        result = backend.decide("test-agent", "簡報", "策略")
        check("b2-1a：decide 回 dict", isinstance(result, dict))
        check("b2-1a：actions 有 1 筆", len(result.get("actions", [])) == 1,
              f"got={result.get('actions')}")
        check("b2-1a：BUY NVDA", result.get("actions", [{}])[0].get("symbol") == "NVDA")
    except Exception as exc:
        check("b2-1a：decide OK", False, repr(exc))
    finally:
        ok_server.shutdown()

    # ── b2-驗收 1b：decide 回 ```json 圍欄 → 仍解析成功 ─────────────────────
    print("\n[b2-1b] decide: ```json 圍欄 → 仍解析成功")
    fenced = f"```json\n{_VALID_DECIDE}\n```"
    fence_server, fence_port = _start_server(
        _make_handler(_openai_response(fenced))
    )
    os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{fence_port}"
    try:
        backend2 = arena.LocalBackend()
        result2 = backend2.decide("test-agent", "簡報", "策略")
        check("b2-1b：圍欄後 actions 解析成功",
              isinstance(result2.get("actions"), list) and not result2.get("backend_error"),
              f"got={result2}")
    except Exception as exc:
        check("b2-1b：圍欄後解析", False, repr(exc))
    finally:
        fence_server.shutdown()

    # ── b2-驗收 1c：decide 回含 <think> 前綴 → 剝除後解析成功 ────────────────
    print("\n[b2-1c] decide: <think> 前綴 → 剝除後解析成功")
    think_content = f"<think>思考中...</think>\n{_VALID_DECIDE}"
    think_server, think_port = _start_server(
        _make_handler(_openai_response(think_content))
    )
    os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{think_port}"
    try:
        backend3 = arena.LocalBackend()
        result3 = backend3.decide("test-agent", "簡報", "策略")
        check("b2-1c：<think> 剝除後解析成功",
              isinstance(result3.get("actions"), list) and not result3.get("backend_error"),
              f"got={result3}")
    except Exception as exc:
        check("b2-1c：<think> 剝除後解析", False, repr(exc))
    finally:
        think_server.shutdown()

    # ── b2-驗收 2a：decide 畸形 JSON → backend_error=True 不拋例外 ────────────
    print("\n[b2-2a] decide: 畸形 JSON → backend_error=True 不拋例外")
    bad_server, bad_port = _start_server(
        _make_handler(_openai_response("NOT_JSON_AT_ALL"))
    )
    os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{bad_port}"
    try:
        backend4 = arena.LocalBackend()
        result4 = backend4.decide("test-agent", "簡報", "策略")
        check("b2-2a：回 dict 不拋例外", isinstance(result4, dict))
        check("b2-2a：backend_error=True", result4.get("backend_error") is True,
              f"got={result4}")
        check("b2-2a：actions=[]", result4.get("actions") == [],
              f"got={result4.get('actions')}")
    except Exception as exc:
        check("b2-2a：不拋例外", False, repr(exc))
    finally:
        bad_server.shutdown()

    # ── b2-驗收 2b：reflect 畸形 JSON → 失敗回三空欄 ─────────────────────────
    print("\n[b2-2b] reflect: 畸形 JSON → 失敗回三空欄")
    bad2_server, bad2_port = _start_server(
        _make_handler(_openai_response("ALSO_NOT_JSON"))
    )
    os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{bad2_port}"
    try:
        backend5 = arena.LocalBackend()
        result5 = backend5.reflect("test-agent", "dossier")
        check("b2-2b：reflect 回 dict 不拋例外", isinstance(result5, dict))
        check("b2-2b：public_letter=''",  result5.get("public_letter") == "")
        check("b2-2b：reflection_md=''",  result5.get("reflection_md") == "")
        check("b2-2b：strategy_md=''",    result5.get("strategy_md") == "")
    except Exception as exc:
        check("b2-2b：不拋例外", False, repr(exc))
    finally:
        bad2_server.shutdown()

    # ── b2-驗收 3：假 server 未啟動 → LocalBackend() raise RuntimeError ────────
    print("\n[b2-3] 假 server 未啟動 → LocalBackend() raise RuntimeError")
    os.environ["LOCAL_LLM_BASE_URL"] = "http://127.0.0.1:19997"
    raised_rt = False
    msg_ok = False
    try:
        arena.LocalBackend()
    except RuntimeError as exc:
        raised_rt = True
        msg_ok = "本地模型未啟動" in str(exc)
    except Exception as exc:
        pass
    check("b2-3：raise RuntimeError", raised_rt)
    check("b2-3：訊息含「本地模型未啟動」", msg_ok)

    # 清除 env，避免影響後續
    os.environ.pop("LOCAL_LLM_BASE_URL", None)
    os.environ.pop("LOCAL_LLM_MODEL", None)

    # ── b2-驗收 4：_resolve_backend_name ──────────────────────────────────────
    print("\n[b2-4] _resolve_backend_name：旗標 > settings > 預設")
    rb = arena._resolve_backend_name

    # 旗標優先
    check("b2-4：flag=gemini", rb("gemini") == "gemini")
    check("b2-4：flag=local",  rb("local") == "local")

    # 預設 gemini（無旗標、無 env）
    os.environ.pop("ARENA_BACKEND", None)
    check("b2-4：預設=gemini", rb(None) == "gemini")

    # settings env 優先（flag=None，但 env 有值）
    os.environ["ARENA_BACKEND"] = "local"
    check("b2-4：settings=local",  rb(None) == "local")
    os.environ.pop("ARENA_BACKEND", None)

    # 非法值 → raise / exit（函式直接 raise ValueError）
    raised_invalid = False
    try:
        rb("invalid_backend")
    except (ValueError, SystemExit):
        raised_invalid = True
    check("b2-4：非法值 → raise", raised_invalid)

    # ── b2-驗收 5：run_daily 端到端（tempfile DB + 假 server 回一筆合法 BUY）─
    print("\n[b2-5] run_daily 端到端（LocalBackend + tempfile DB）")

    # 準備 tempfile DB
    d5_tmpdir = Path(tempfile.mkdtemp(prefix="arena_local_test_"))
    d5_db = d5_tmpdir / "test.sqlite"
    d5_con = sqlite3.connect(str(d5_db))
    d5_con.row_factory = sqlite3.Row
    arena.migrate(d5_con)
    arena.seed_agents(d5_con)

    # arena.migrate 不建 prices 表；手動建立
    d5_con.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, UNIQUE(symbol, date)
        );
    """)

    # 插入 NVDA 假價格（yesterday + today）
    AS_OF_D5 = "2026-01-11"
    d5_con.executescript("""
        INSERT OR IGNORE INTO prices (symbol, date, open, high, low, close, volume)
        VALUES ('NVDA', '2026-01-10', 900, 910, 890, 905, 1000000);
        INSERT OR IGNORE INTO prices (symbol, date, open, high, low, close, volume)
        VALUES ('NVDA', '2026-01-11', 905, 920, 900, 915, 1200000);
        INSERT OR IGNORE INTO prices (symbol, date, open, high, low, close, volume)
        VALUES ('SOXX', '2026-01-10', 200, 205, 198, 202, 500000);
        INSERT OR IGNORE INTO prices (symbol, date, open, high, low, close, volume)
        VALUES ('SOXX', '2026-01-11', 202, 210, 200, 208, 600000);
        INSERT OR IGNORE INTO prices (symbol, date, open, high, low, close, volume)
        VALUES ('SPY',  '2026-01-10', 500, 510, 495, 505, 2000000);
        INSERT OR IGNORE INTO prices (symbol, date, open, high, low, close, volume)
        VALUES ('SPY',  '2026-01-11', 505, 515, 500, 510, 2200000);
    """)
    d5_con.commit()

    # 啟動假 server
    d5_server, d5_port = _start_server(
        _make_handler(_openai_response(_VALID_DECIDE))
    )
    os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{d5_port}"
    os.environ["LOCAL_LLM_MODEL"] = "qwen3:14b"

    try:
        local_backend = arena.LocalBackend()
        arena.run_daily(d5_con, AS_OF_D5, local_backend)

        # 驗收：agent_trades 有當日決策記錄（pending / executed）
        orders = d5_con.execute(
            "SELECT * FROM agent_trades WHERE decided_date=?", (AS_OF_D5,)
        ).fetchall()
        check("b2-5：agent_trades 有記錄", len(orders) > 0, f"count={len(orders)}")

        # agent_nav_daily 有當日 NAV 列
        navs = d5_con.execute(
            "SELECT * FROM agent_nav_daily WHERE date=?", (AS_OF_D5,)
        ).fetchall()
        check("b2-5：agent_nav_daily 有記錄", len(navs) > 0, f"count={len(navs)}")

    except Exception as exc:
        check("b2-5：run_daily 不拋例外", False, repr(exc))
    finally:
        d5_server.shutdown()
        os.environ.pop("LOCAL_LLM_BASE_URL", None)
        os.environ.pop("LOCAL_LLM_MODEL", None)
        d5_con.close()

    return finish()


if __name__ == "__main__":
    sys.exit(main())
