# -*- coding: utf-8 -*-
"""
serenity/services/health.py
資料時效自檢服務（/api/health + /api/admin/refresh + /api/admin/refresh/status）

設計原則：
- 單一事實來源：用 importlib 從 scripts/daily_check.py 載入 run_all_checks，不複製門檻邏輯。
- SAFE_DOMAINS：可自動補抓的安全域（不需 cookies/Playwright/Gemini）。
- run_refresh()：依序執行各域的 in-process 補抓，每步寫 job_runs，用 threading.Lock 保護狀態。
"""
import importlib.util
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import DB_PATH, ROOT
from ..services.signal import snapshot_signals

# ── 安全域（永不自動跑：tweets / expert_views / arena_nav）──────────────────────
SAFE_DOMAINS = [
    "prices",
    "benchmarks",
    "signal_history",
    "news",
    "stocktwits",
    "fundamentals",
    "estimates",
]

# ── 模組級狀態（仿 bootstrap.py）────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "running": False,
    "steps": [],  # list of {"name", "status", "detail"}
}


# ── 動態載入輔助 ────────────────────────────────────────────────────────────────

def _load_daily_check():
    """importlib 載入 ROOT/scripts/daily_check.py，回傳模組物件。"""
    spec = importlib.util.spec_from_file_location(
        "daily_check", ROOT / "scripts" / "daily_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_ingest():
    """importlib 載入 ROOT/scripts/ingest.py，回傳模組物件。"""
    spec = importlib.util.spec_from_file_location(
        "ingest", ROOT / "scripts" / "ingest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 主要公開函式 ────────────────────────────────────────────────────────────────

def health_payload() -> dict:
    """
    GET /api/health → 呼叫 daily_check.run_all_checks，回傳標準化 payload。
    格式：{"as_of": str, "ok": bool, "checked_at": UTC ISO str, "checks": [...]}
    """
    dc = _load_daily_check()
    checks, today_str = dc.run_all_checks(DB_PATH)
    all_ok = all(c["status"] == "ok" for c in checks)
    return {
        "as_of": today_str,
        "ok": all_ok,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


def get_stale_domains() -> tuple[list[str], list[str]]:
    """
    唯讀計算目前過期域，回傳 (safe_stale, manual_stale)。
    safe_stale：過期∩SAFE_DOMAINS，依 SAFE_DOMAINS 順序。
    manual_stale：過期∖SAFE_DOMAINS（需排程/開發者功能）。
    """
    dc = _load_daily_check()
    checks, _ = dc.run_all_checks(DB_PATH)
    stale = {c["name"] for c in checks if c["status"] != "ok"}
    safe_set = set(SAFE_DOMAINS)
    # 依 SAFE_DOMAINS 定義順序
    safe_stale = [d for d in SAFE_DOMAINS if d in stale]
    manual_stale = sorted(stale - safe_set)
    return safe_stale, manual_stale


def refresh_status() -> dict:
    """GET /api/admin/refresh/status → {"running": bool, "steps": [...]}"""
    with _lock:
        return {
            "running": _state["running"],
            "steps": list(_state["steps"]),
        }


# ── job_runs 記錄輔助 ────────────────────────────────────────────────────────────

def _ensure_job_runs_table(con: sqlite3.Connection) -> None:
    """確保 job_runs 表存在（欄位與 daily_check.py 一致）。"""
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job TEXT NOT NULL,
            mode TEXT NOT NULL,
            cmd TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            returncode INTEGER,
            ok INTEGER,
            error_tail TEXT
        )
    """)
    con.commit()


def _record_job_run(
    mode: str, step: str, status: str, detail: str,
    started_at: str, finished_at: str,
) -> None:
    """將結果記錄到 job_runs 表（in-process 呼叫；cmd 標記為 in-process）。"""
    try:
        con = sqlite3.connect(str(DB_PATH))
        _ensure_job_runs_table(con)
        ok_int = 1 if status == "done" else 0
        con.execute(
            "INSERT INTO job_runs(job, mode, cmd, started_at, finished_at, returncode, ok, error_tail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (step, mode, f"in-process:{step}", started_at, finished_at, 0 if ok_int else 1, ok_int, detail or None),
        )
        con.commit()
        con.close()
    except Exception:
        pass  # job_runs 失敗不影響主流程


# ── in-process 執行各域 ──────────────────────────────────────────────────────────

def _set_step(name: str, status: str, detail: str = "") -> None:
    with _lock:
        for s in _state["steps"]:
            if s["name"] == name:
                s["status"] = status
                s["detail"] = detail
                break


def _run_domain(name: str, ingest, mode: str) -> None:
    """執行單一安全域的補抓動作，更新步驟狀態並寫 job_runs。"""
    _set_step(name, "running")
    started_at = datetime.now(timezone.utc).isoformat()
    detail = ""
    status = "done"
    try:
        if name == "prices":
            ingest.fetch_prices()
        elif name == "benchmarks":
            ingest.fetch_benchmarks()
        elif name == "news":
            ingest.fetch_news()
        elif name == "stocktwits":
            # fetch_stocktwits_all 全失敗時可能 sys.exit(1)：接住 SystemExit 記為失敗
            try:
                ingest.fetch_stocktwits_all()
            except SystemExit as se:
                raise RuntimeError(f"fetch_stocktwits_all exited with code {se.code}") from se
        elif name == "fundamentals":
            ingest.fetch_fundamentals()
        elif name == "estimates":
            ingest.fetch_estimates()
        elif name == "signal_history":
            snapshot_signals()
        else:
            detail = f"未知域：{name}"
            status = "error"
    except Exception as exc:
        detail = str(exc)[:400]
        status = "error"

    finished_at = datetime.now(timezone.utc).isoformat()
    _set_step(name, status, detail)
    _record_job_run(mode, name, status, detail, started_at, finished_at)


def _run_refresh_thread(domains: list[str], mode: str) -> None:
    """背景執行緒：依序補抓各域，完成後解除 running 標記。"""
    try:
        ingest = _load_ingest()
        for name in domains:
            _run_domain(name, ingest, mode)
    except Exception as exc:
        print(f"[Health] run_refresh fatal: {exc}")
    finally:
        with _lock:
            _state["running"] = False


def run_refresh(domains: list[str], mode: str) -> None:
    """
    依序補抓 domains 中各域（in-process），在背景執行緒中執行。
    mode 傳 "ui-refresh" 或 "auto"。
    呼叫前需確認 _state["running"] == False（由呼叫方負責）。
    """
    with _lock:
        _state["running"] = True
        _state["steps"] = [{"name": n, "status": "pending", "detail": ""} for n in domains]

    t = threading.Thread(target=_run_refresh_thread, args=(domains, mode), daemon=True)
    t.start()


def is_running() -> bool:
    """回傳目前是否有 refresh 正在執行。"""
    with _lock:
        return _state["running"]
