"""
serenity/services/bootstrap.py
Bootstrap API — 首次啟動時抓取初始資料（prices / benchmarks / news）

GET  /api/admin/bootstrap/status → {"running": bool, "steps": [...]}
POST /api/admin/bootstrap         → dry_run 回計畫；否則背景執行緒跑 ingest

模組級狀態物件 + threading.Lock（執行緒安全）。
"""
import importlib.util
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT
from ..db import db

# ── 模組級狀態 ─────────────────────────────────────────────────────────────────

_lock = threading.Lock()

_state = {
    "running": False,
    "steps": [],       # list of {"name", "status", "detail"}
}

_PLAN = ["prices", "benchmarks", "news"]


def get_status() -> dict:
    """回傳目前 bootstrap 狀態（thread-safe 快照）。"""
    with _lock:
        return {
            "running": _state["running"],
            "steps": list(_state["steps"]),
        }


def handle_post_bootstrap(payload: dict) -> tuple[dict, int]:
    """
    POST /api/admin/bootstrap 主邏輯。
    - dry_run=true  → 回傳計畫步驟，不執行
    - otherwise     → 若已在跑回 409；否則開背景執行緒
    回傳 (response_dict, http_status_code)
    """
    if payload.get("dry_run"):
        return {"steps": _PLAN, "dry_run": True}, 200

    with _lock:
        if _state["running"]:
            return {"error": "already running"}, 409
        # 重置狀態
        _state["running"] = True
        _state["steps"] = [{"name": n, "status": "pending", "detail": ""} for n in _PLAN]

    t = threading.Thread(target=_run_bootstrap, daemon=True)
    t.start()
    return {"started": True, "steps": _PLAN}, 200


# ── 內部：背景執行緒 ────────────────────────────────────────────────────────────

def _load_ingest():
    """動態載入 ROOT/scripts/ingest.py（仿 quant.py 模式）。"""
    spec = importlib.util.spec_from_file_location("ingest", ROOT / "scripts" / "ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _set_step(name: str, status: str, detail: str = "") -> None:
    with _lock:
        for s in _state["steps"]:
            if s["name"] == name:
                s["status"] = status
                s["detail"] = detail
                break


def _record_job(con, mode: str, step: str, status: str, detail: str) -> None:
    """將結果記錄到 job_runs 表（mode="bootstrap"）。"""
    try:
        con.execute(
            "INSERT OR IGNORE INTO job_runs (mode, step, status, detail, ran_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mode, step, status, detail, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    except Exception:
        pass  # job_runs 表不存在也不影響主流程


def _run_bootstrap() -> None:
    """依序跑 prices → benchmarks → news，每步更新狀態與 job_runs。"""
    try:
        ingest = _load_ingest()
        con = db()

        step_funcs = {
            "prices":     lambda: ingest.fetch_prices(),
            "benchmarks": lambda: ingest.fetch_benchmarks(),
            "news":       lambda: ingest.fetch_news(),
        }

        for name in _PLAN:
            _set_step(name, "running")
            fn = step_funcs.get(name)
            try:
                if fn:
                    fn()
                _set_step(name, "done")
                _record_job(con, "bootstrap", name, "done", "")
            except Exception as exc:
                detail = str(exc)[:200]
                _set_step(name, "error", detail)
                _record_job(con, "bootstrap", name, "error", detail)

        con.close()
    except Exception as exc:
        print(f"[Bootstrap] fatal error: {exc}")
    finally:
        with _lock:
            _state["running"] = False
