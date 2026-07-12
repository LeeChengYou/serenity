# -*- coding: utf-8 -*-
"""
(b) 本地 LLM backend — 驗收測試（規格：docs/REQUIREMENTS_AI_MARKET.md b-驗收 1~6）

執行：PYTHONIOENCODING=utf-8 python scratch/test_local_llm.py
原則：
  - 用 threading + http.server 起本機假 Ollama（回 OpenAI 格式 JSON）
  - 涵蓋正常/畸形 JSON/連不上/is_up/handle_chat_api local 路徑/LocalConsultBackend
  - 不打真 Gemini、不打真 Ollama；不動正式 data/serenity.sqlite
"""
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

# 讓 config.py 不載入 .env，避免側效應
os.environ.setdefault("SERENITY_NO_DOTENV", "1")

RESULTS = []


def check(name: str, cond, detail: str = ""):
    ok = bool(cond)
    RESULTS.append((name, ok))
    mark = "  OK " if ok else "! FAIL"
    print(f"{mark}  {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    return ok


def finish():
    passed = sum(1 for _, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("=" * 70)
    print(f"Local LLM Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# 假 Ollama HTTP server 工廠
# ---------------------------------------------------------------------------

def _make_handler(response_body: bytes, status: int = 200):
    """回傳一個 BaseHTTPRequestHandler 類別，固定回應指定 body。"""
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 靜默

        def do_GET(self):
            # /api/tags 健檢
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


def _start_server(handler_class) -> tuple[HTTPServer, int]:
    """在隨機高位 port 啟動假 server，回傳 (server, port)。"""
    server = HTTPServer(("127.0.0.1", 0), handler_class)  # port=0 → OS 分配
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# ---------------------------------------------------------------------------
# 標準 OpenAI 回應格式
# ---------------------------------------------------------------------------

def _openai_response(content: str) -> bytes:
    body = {
        "choices": [
            {"message": {"role": "assistant", "content": content}}
        ]
    }
    return json.dumps(body).encode("utf-8")


# ---------------------------------------------------------------------------
# 測試主體
# ---------------------------------------------------------------------------

def main():
    from serenity.llm_local import LocalLLMUnavailable, call_local_llm, is_local_llm_up

    # ── b-驗收 1：call_local_llm 對假 server 正常解析 ────────────────────
    expected_reply = "你好，這是本地 LLM 回覆。"
    ok_server, ok_port = _start_server(
        _make_handler(_openai_response(expected_reply))
    )
    try:
        result = call_local_llm(
            messages=[{"role": "user", "content": "test"}],
            base_url=f"http://127.0.0.1:{ok_port}",
            model="qwen3:14b",
        )
        check("b-驗收1：正常解析回覆文字", result == expected_reply,
              f"got={result!r}")
    finally:
        ok_server.shutdown()

    # ── b-驗收 2a：假 server 回畸形 JSON → raise（不回假值）────────────────
    bad_server, bad_port = _start_server(
        _make_handler(b"NOT_JSON_AT_ALL")
    )
    try:
        raised = False
        try:
            call_local_llm(
                messages=[{"role": "user", "content": "test"}],
                base_url=f"http://127.0.0.1:{bad_port}",
                model="qwen3:14b",
            )
        except Exception:
            raised = True
        check("b-驗收2a：畸形 JSON → 顯式 raise", raised)
    finally:
        bad_server.shutdown()

    # ── b-驗收 2b：假 server 回缺欄位 JSON → raise ───────────────────────
    missing_field_body = json.dumps({"choices": []}).encode("utf-8")
    mf_server, mf_port = _start_server(
        _make_handler(missing_field_body)
    )
    try:
        raised = False
        try:
            call_local_llm(
                messages=[{"role": "user", "content": "test"}],
                base_url=f"http://127.0.0.1:{mf_port}",
                model="qwen3:14b",
            )
        except Exception:
            raised = True
        check("b-驗收2b：缺欄位 JSON → 顯式 raise", raised)
    finally:
        mf_server.shutdown()

    # ── b-驗收 3：連不上（未啟動）→ LocalLLMUnavailable，訊息含「本地模型未啟動」
    raised_correct = False
    msg_correct = False
    try:
        call_local_llm(
            messages=[{"role": "user", "content": "test"}],
            base_url="http://127.0.0.1:19999",  # 不太可能有 server
            model="qwen3:14b",
            timeout=2,
        )
    except LocalLLMUnavailable as exc:
        raised_correct = True
        msg_correct = "本地模型未啟動" in str(exc)
    except Exception:
        pass
    check("b-驗收3：連不上 → LocalLLMUnavailable", raised_correct)
    check("b-驗收3：錯誤訊息含「本地模型未啟動」", msg_correct)

    # ── b-驗收 4：is_local_llm_up 有假 server → True；無 → False ─────────
    tags_server, tags_port = _start_server(
        _make_handler(json.dumps({"models": []}).encode("utf-8"))
    )
    try:
        up = is_local_llm_up(base_url=f"http://127.0.0.1:{tags_port}")
        check("b-驗收4：有假 server → is_local_llm_up True", up)
    finally:
        tags_server.shutdown()

    down = is_local_llm_up(base_url="http://127.0.0.1:19998")
    check("b-驗收4：無 server → is_local_llm_up False", not down)

    # ── b-驗收 5：handle_chat_api(model='local') ──────────────────────────
    # 5a: 假 server 開啟 → 成功回覆
    chat_server, chat_port = _start_server(
        _make_handler(_openai_response("這是 handle_chat_api 走本地的回覆"))
    )
    try:
        os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{chat_port}"
        os.environ["LOCAL_LLM_MODEL"] = "qwen3:14b"
        # 需要重新載入 config 使 env 生效（get_setting 是 function，直接讀 os.environ）
        from serenity.services.chat import handle_chat_api
        payload = {
            "model": "local",
            "messages": [{"role": "user", "content": "大盤如何？"}],
        }
        res = handle_chat_api(payload)
        check("b-驗收5a：model=local 假 server 開→成功回覆", "response" in res and res.get("response"),
              f"res={res}")
        check("b-驗收5a：回傳無 error 欄", "error" not in res)
    finally:
        chat_server.shutdown()

    # 5b: 假 server 關閉 → 回傳含 error 的 dict（非例外）
    # chat_server 已關閉，再呼叫一次
    res2 = handle_chat_api({
        "model": "local",
        "messages": [{"role": "user", "content": "大盤如何？"}],
    })
    check("b-驗收5b：假 server 關→回傳含 error dict", "error" in res2,
          f"res2={res2}")
    check("b-驗收5b：不拋例外（200+error 欄位）", isinstance(res2, dict))

    # 清除環境變數
    os.environ.pop("LOCAL_LLM_BASE_URL", None)
    os.environ.pop("LOCAL_LLM_MODEL", None)

    # ── b-驗收 6：LocalConsultBackend + run_consult 全流程落庫 ────────────
    # 用 tempfile DB 副本，正式 DB 只讀
    import fund_pool as fp

    src_db_path = ROOT / "data" / "serenity.sqlite"
    tmpdir = Path(tempfile.mkdtemp(prefix="local_llm_test_"))
    test_db_path = tmpdir / "test.sqlite"

    # 複製 src DB（若存在），再補齊 fund_pool.py 需要的最小 schema
    if src_db_path.exists():
        src = sqlite3.connect(src_db_path)
        dst = sqlite3.connect(test_db_path)
        src.backup(dst)
        src.close()
        dst.close()

    # 補齊 fund_pool / arena 需要的最小 schema（冪等 CREATE IF NOT EXISTS）
    con_tmp = sqlite3.connect(test_db_path)
    con_tmp.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, domain TEXT, style_seed TEXT,
            backend TEXT, status TEXT, relaunches INTEGER, hwm REAL, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_state (
            agent_id TEXT, month TEXT, cash REAL, nav REAL, updated_at TEXT,
            UNIQUE(agent_id, month)
        );
        CREATE TABLE IF NOT EXISTS agent_positions (
            agent_id TEXT, symbol TEXT, qty REAL, avg_cost REAL, updated_at TEXT,
            UNIQUE(agent_id, symbol)
        );
        CREATE TABLE IF NOT EXISTS agent_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT, decided_date TEXT, exec_date TEXT,
            symbol TEXT, side TEXT, qty REAL, price REAL, usd REAL,
            cash_after REAL, reason TEXT, status TEXT, briefing_path TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT, content TEXT
        );
        CREATE TABLE IF NOT EXISTS prices (
            symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL
        );
    """)
    # 確保至少有一筆 prices（若 prices 完全空）
    if con_tmp.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0:
        con_tmp.execute(
            "INSERT INTO prices VALUES ('NVDA','2026-01-10',500,510,490,505,1000000)"
        )
    con_tmp.commit()
    con_tmp.close()

    con = sqlite3.connect(test_db_path)
    con.row_factory = sqlite3.Row
    fp.migrate(con)

    # 建立測試資金池
    pool_id = fp.create_pool(con, "test-local-llm", 10000.0, created_at="2026-01-01T00:00:00")

    # 確認 LocalConsultBackend 存在且繼承 ConsultBackend
    check("b-驗收6：LocalConsultBackend 存在", hasattr(fp, "LocalConsultBackend"))
    check("b-驗收6：LocalConsultBackend 繼承 ConsultBackend",
          issubclass(fp.LocalConsultBackend, fp.ConsultBackend))

    # 啟動假 Ollama server 回標準 opine JSON
    opine_json = json.dumps({
        "stance": "support",
        "confidence": 0.8,
        "opinion": "本地 LLM 測試意見",
    }).encode("utf-8")
    local_consult_server, lc_port = _start_server(
        _make_handler(_openai_response(json.dumps({
            "stance": "support",
            "confidence": 0.8,
            "opinion": "本地 LLM 測試意見",
        })))
    )
    try:
        os.environ["LOCAL_LLM_BASE_URL"] = f"http://127.0.0.1:{lc_port}"
        os.environ["LOCAL_LLM_MODEL"] = "qwen3:14b"

        local_backend = fp.LocalConsultBackend()

        # 找一個存在的 agent（從 agents 表）；若沒有則用 stub participants
        agent_rows = con.execute("SELECT id FROM agents WHERE backend != 'human' LIMIT 2").fetchall()
        if agent_rows:
            participants = [r[0] for r in agent_rows]
        else:
            # 插入假 agent
            con.execute(
                "INSERT OR IGNORE INTO agents (id, domain, style_seed, backend, status, relaunches, hwm, created_at) "
                "VALUES ('semis-momentum','semis','momentum','gemini','active',0,10000.0,'2026-01-01')"
            )
            con.commit()
            participants = ["semis-momentum"]

        consult_id = fp.run_consult(
            con, pool_id, "NVDA 現在適合買入嗎？",
            "NVDA", participants, "2026-01-10", local_backend,
        )
        check("b-驗收6：run_consult 回傳 int consult_id", isinstance(consult_id, int))

        row = con.execute("SELECT * FROM pool_consults WHERE id=?", (consult_id,)).fetchone()
        check("b-驗收6：pool_consults 落庫成功", row is not None)

        opinions = con.execute(
            "SELECT * FROM pool_consult_opinions WHERE consult_id=?", (consult_id,)
        ).fetchall()
        check("b-驗收6：pool_consult_opinions 有記錄", len(opinions) > 0)

    finally:
        local_consult_server.shutdown()
        os.environ.pop("LOCAL_LLM_BASE_URL", None)
        os.environ.pop("LOCAL_LLM_MODEL", None)

    con.close()

    return finish()


if __name__ == "__main__":
    sys.exit(main())
