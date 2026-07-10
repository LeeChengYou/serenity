# -*- coding: utf-8 -*-
"""daily_check.py 驗收測試（由主對話先行定義，實作必須通過全部案例）

執行：PYTHONIOENCODING=utf-8 python scratch/test_daily_check.py
通過標準：0 failed、exit 0。
注意：本測試絕不真正執行 run/repair（只用 check 與 --dry-run），不打真 Gemini、不寫真 DB。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
SCRIPT = ROOT / "scripts" / "daily_check.py"
REAL_DB = ROOT / "data" / "serenity.sqlite"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

REQUIRED_CHECKS = {
    "prices", "benchmarks", "signal_history", "news", "stocktwits",
    "tweets", "fundamentals", "estimates", "expert_views", "arena_nav",
}

results = []


def record(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok))
    print(("PASS" if ok else "FAIL"), name, ("" if ok else f"| {detail}"))


def run_cli(*args, timeout=300):
    return subprocess.run(
        [PY, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=ENV, cwd=str(ROOT), timeout=timeout,
    )


# ---- T0 語法 ----
r = subprocess.run([PY, "-m", "py_compile", str(SCRIPT)],
                   capture_output=True, text=True, env=ENV)
record("T0 py_compile 通過", r.returncode == 0, r.stderr[-300:])

# ---- T1 check --json 於真實 DB（唯讀） ----
r = run_cli("check", "--json")
record("T1a exit code 為 0 或 1", r.returncode in (0, 1),
       f"rc={r.returncode} stderr={r.stderr[-300:]}")
data = None
try:
    data = json.loads(r.stdout)
except Exception:
    pass
record("T1b stdout 是純 JSON（--json 不得混雜其他輸出）", data is not None,
       r.stdout[:300])
if data:
    names = {c["name"] for c in data.get("checks", [])}
    record("T1c 十項檢查齊全", REQUIRED_CHECKS <= names,
           f"缺: {REQUIRED_CHECKS - names}")
    record("T1d status 值僅限 ok/stale/missing",
           all(c.get("status") in ("ok", "stale", "missing")
               for c in data.get("checks", [])),
           str([(c.get("name"), c.get("status")) for c in data.get("checks", [])]))
    con = sqlite3.connect(str(REAL_DB))
    real_latest = con.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    con.close()
    pc = next((c for c in data["checks"] if c["name"] == "prices"), {})
    record("T1e prices.latest 可對回 DB（零捏造）", pc.get("latest") == real_latest,
           f"回報 {pc.get('latest')} vs DB {real_latest}")
    record("T1f exit code 與 JSON ok 欄位一致",
           (r.returncode == 0) == bool(data.get("ok")),
           f"rc={r.returncode} ok={data.get('ok')}")

# ---- 準備污染過的暫存 DB（news 變舊） ----
tmpdir = tempfile.mkdtemp(prefix="daily_check_test_")
tmp_db = Path(tmpdir) / "test.sqlite"
shutil.copy(str(REAL_DB), str(tmp_db))
con = sqlite3.connect(str(tmp_db))
con.execute("DELETE FROM news WHERE fetched_at >= datetime('now','-3 day')")
con.commit()
con.close()

# ---- T2 check 應偵測 news 過期 ----
r = run_cli("check", "--json", "--db", str(tmp_db))
data2 = json.loads(r.stdout) if r.stdout.strip().startswith("{") else None
record("T2a 污染 DB 的 check 可解析", data2 is not None, r.stdout[:300])
if data2:
    nc = next((c for c in data2["checks"] if c["name"] == "news"), {})
    record("T2b news 被標為非 ok", nc.get("status") in ("stale", "missing"), str(nc))
    record("T2c 整體 exit 1", r.returncode == 1, f"rc={r.returncode}")

# ---- T3 repair --dry-run：列出修復計畫、不執行、不寫 job_runs ----
def _job_runs_count(db_file):
    con = sqlite3.connect(str(db_file))
    has_tbl = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='job_runs'").fetchone()
    n = con.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] if has_tbl else 0
    con.close()
    return n

n_jobs_before = _job_runs_count(tmp_db)
r = run_cli("repair", "--dry-run", "--db", str(tmp_db))
norm = r.stdout.replace("\\", "/")
record("T3a dry-run exit 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-300:]}")
record("T3b 修復計畫包含 news 對應指令（scripts/ingest.py news）",
       "scripts/ingest.py news" in norm, norm[:800])
n_jobs_after = _job_runs_count(tmp_db)
record("T3c dry-run 不寫入 job_runs（增量=0）", n_jobs_after == n_jobs_before,
       f"before={n_jobs_before} after={n_jobs_after}")

# ---- T4 run --dry-run：完整每日流程與順序 ----
r = run_cli("run", "--dry-run", "--db", str(tmp_db))
norm = r.stdout.replace("\\", "/")
record("T4a exit 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-300:]}")
daily_keys = ["scripts/ingest.py prices", "scripts/ingest.py benchmarks",
              "--snapshot-once", "scripts/ingest.py news",
              "scripts/ingest.py stocktwits", "scripts/agent_arena.py daily"]
missing = [k for k in daily_keys if k not in norm]
record("T4b 每日六步（J-1/J-8/J-2/J-5/J-3/J-10）都在計畫中", not missing,
       f"缺: {missing}")
if not missing:
    record("T4c 依賴順序：prices 先於 snapshot 先於 arena",
           norm.index("scripts/ingest.py prices")
           < norm.index("--snapshot-once")
           < norm.index("scripts/agent_arena.py daily"))

# ---- T5 安全閥：非預設 DB 不允許真正執行 ----
r = run_cli("repair", "--db", str(tmp_db))
record("T5 非預設 DB 且非 dry-run 必須拒絕（exit 2）", r.returncode == 2,
       f"rc={r.returncode} out={r.stdout[:200]}")

# ---- T6 冪等：check 連跑兩次結果一致 ----
r1 = run_cli("check", "--json")
r2 = run_cli("check", "--json")
record("T6 check 連跑兩次 exit code 一致", r1.returncode == r2.returncode,
       f"{r1.returncode} vs {r2.returncode}")

# ---- 收尾 ----
shutil.rmtree(tmpdir, ignore_errors=True)
failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
