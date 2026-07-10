#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/daily_check.py — 每日資料健康檢查 + 全流程執行 + 斷點修復工具

用法：
  python scripts/daily_check.py check  [--db PATH] [--json]
  python scripts/daily_check.py run    [--db PATH] [--dry-run]
  python scripts/daily_check.py repair [--db PATH] [--dry-run]

Exit code:
  check: 全 ok=0, 否則=1
  run/repair: 全部成功且終檢全 ok=0, 有斷點=1
  dry-run: 一律 0
  非預設 DB + 非 dry-run: exit 2
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "serenity.sqlite"
LOG_DIR = ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "daily_check.log"

# ──────────────────────────────────────────────────────────────────────────────
# 輔助：計算台北時間今天與 expected_td（最近一個早於 today 的平日）
# ──────────────────────────────────────────────────────────────────────────────

def _taipei_today() -> str:
    """回傳台北時間今天的 YYYY-MM-DD 字串。"""
    tz_taipei = timezone(timedelta(hours=8))
    return datetime.now(tz_taipei).strftime("%Y-%m-%d")


def _expected_td(today_str: str) -> str:
    """回傳最近一個嚴格早於 today_str 的平日（週一→上週五）。"""
    from datetime import date
    today = date.fromisoformat(today_str)
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d.isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# 十項檢查
# ──────────────────────────────────────────────────────────────────────────────

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return r is not None


def check_prices(conn: sqlite3.Connection, today_str: str) -> dict:
    """1. prices：MAX(date) >= expected_td 且該日 symbol 數 >= 80。"""
    exp = _expected_td(today_str)
    if not _has_table(conn, "prices"):
        return {"name": "prices", "status": "missing", "latest": None,
                "expected": exp, "detail": "資料表 prices 不存在"}
    row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "prices", "status": "missing", "latest": None,
                "expected": exp, "detail": "prices 表無資料"}
    # 計算該最新日期的 symbol 數
    cnt_row = conn.execute(
        "SELECT COUNT(DISTINCT symbol) FROM prices WHERE date = ?", (latest,)
    ).fetchone()
    cnt = cnt_row[0] if cnt_row else 0
    ok = (latest >= exp) and (cnt >= 80)
    status = "ok" if ok else "stale"
    detail = f"該日 symbol 數={cnt}"
    if latest >= exp and cnt < 80:
        detail += f"（< 80，資料不完整）"
    elif latest < exp:
        detail += f"，最新日 {latest} < expected {exp}"
        if latest < exp:
            days_diff = (
                datetime.fromisoformat(exp) - datetime.fromisoformat(latest)
            ).days
            if days_diff == 1:
                detail += "（可能為美股休市）"
    return {"name": "prices", "status": status, "latest": latest,
            "expected": exp, "detail": detail}


def check_benchmarks(conn: sqlite3.Connection, today_str: str) -> dict:
    """2. benchmarks：SPY/QQQ/SOXX 各自 MAX(date) >= expected_td。"""
    exp = _expected_td(today_str)
    symbols = ["SPY", "QQQ", "SOXX"]
    if not _has_table(conn, "prices"):
        return {"name": "benchmarks", "status": "missing", "latest": None,
                "expected": exp, "detail": "資料表 prices 不存在"}
    latest_map = {}
    for sym in symbols:
        row = conn.execute(
            "SELECT MAX(date) FROM prices WHERE symbol=?", (sym,)
        ).fetchone()
        latest_map[sym] = row[0] if row else None

    all_ok = all(v is not None and v >= exp for v in latest_map.values())
    any_missing = any(v is None for v in latest_map.values())
    status = "ok" if all_ok else ("missing" if any_missing else "stale")
    latest_overall = max((v for v in latest_map.values() if v), default=None)
    detail = ", ".join(f"{k}={v}" for k, v in sorted(latest_map.items()))
    return {"name": "benchmarks", "status": status, "latest": latest_overall,
            "expected": exp, "detail": detail}


def check_signal_history(conn: sqlite3.Connection, today_str: str) -> dict:
    """3. signal_history：MAX(date) == today 且該日列數 >= 79。"""
    if not _has_table(conn, "signal_history"):
        return {"name": "signal_history", "status": "missing", "latest": None,
                "expected": today_str, "detail": "資料表 signal_history 不存在"}
    row = conn.execute("SELECT MAX(date) FROM signal_history").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "signal_history", "status": "missing", "latest": None,
                "expected": today_str, "detail": "signal_history 表無資料"}
    cnt_row = conn.execute(
        "SELECT COUNT(*) FROM signal_history WHERE date=?", (latest,)
    ).fetchone()
    cnt = cnt_row[0] if cnt_row else 0
    ok = (latest == today_str) and (cnt >= 79)
    status = "ok" if ok else "stale"
    detail = f"該日列數={cnt}（需>=79）"
    if latest != today_str:
        detail += f"，最新日 {latest} != today {today_str}"
    return {"name": "signal_history", "status": status, "latest": latest,
            "expected": today_str, "detail": detail}


def check_news(conn: sqlite3.Connection, now_dt: datetime) -> dict:
    """4. news：MAX(fetched_at) 距現在 <= 36 小時。"""
    if not _has_table(conn, "news"):
        return {"name": "news", "status": "missing", "latest": None,
                "expected": "≤36h ago", "detail": "資料表 news 不存在"}
    row = conn.execute("SELECT MAX(fetched_at) FROM news").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "news", "status": "missing", "latest": None,
                "expected": "≤36h ago", "detail": "news 表無資料"}
    try:
        # 解析 ISO 8601（含 Z 或 +HH:MM）
        ts = latest.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now_dt - dt
        ok = diff.total_seconds() <= 36 * 3600
        status = "ok" if ok else "stale"
        hours_ago = diff.total_seconds() / 3600
        detail = f"距現在 {hours_ago:.1f} 小時（需<=36h）"
    except Exception as e:
        return {"name": "news", "status": "stale", "latest": latest,
                "expected": "≤36h ago", "detail": f"時間解析錯誤: {e}"}
    return {"name": "news", "status": status, "latest": latest,
            "expected": "≤36h ago", "detail": detail}


def check_stocktwits(conn: sqlite3.Connection, now_dt: datetime) -> dict:
    """5. stocktwits：news_sentiment MAX(published_at) 距現在 <= 48 小時。"""
    if not _has_table(conn, "news_sentiment"):
        return {"name": "stocktwits", "status": "missing", "latest": None,
                "expected": "≤48h ago", "detail": "資料表 news_sentiment 不存在"}
    row = conn.execute("SELECT MAX(published_at) FROM news_sentiment").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "stocktwits", "status": "missing", "latest": None,
                "expected": "≤48h ago", "detail": "news_sentiment 表無資料"}
    try:
        ts = latest.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now_dt - dt
        ok = diff.total_seconds() <= 48 * 3600
        status = "ok" if ok else "stale"
        hours_ago = diff.total_seconds() / 3600
        detail = f"距現在 {hours_ago:.1f} 小時（需<=48h）"
    except Exception as e:
        return {"name": "stocktwits", "status": "stale", "latest": latest,
                "expected": "≤48h ago", "detail": f"時間解析錯誤: {e}"}
    return {"name": "stocktwits", "status": status, "latest": latest,
            "expected": "≤48h ago", "detail": detail}


def check_tweets(conn: sqlite3.Connection, now_dt: datetime) -> dict:
    """6. tweets：MAX(created_at) 距現在 <= 7 天。"""
    if not _has_table(conn, "tweets"):
        return {"name": "tweets", "status": "missing", "latest": None,
                "expected": "≤7d ago", "detail": "資料表 tweets 不存在"}
    row = conn.execute("SELECT MAX(created_at) FROM tweets").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "tweets", "status": "missing", "latest": None,
                "expected": "≤7d ago", "detail": "tweets 表無資料"}
    try:
        ts = latest.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now_dt - dt
        ok = diff.total_seconds() <= 7 * 86400
        status = "ok" if ok else "stale"
        days_ago = diff.total_seconds() / 86400
        detail = f"距現在 {days_ago:.1f} 天（需<=7d）"
    except Exception as e:
        return {"name": "tweets", "status": "stale", "latest": latest,
                "expected": "≤7d ago", "detail": f"時間解析錯誤: {e}"}
    return {"name": "tweets", "status": status, "latest": latest,
            "expected": "≤7d ago", "detail": detail}


def check_fundamentals(conn: sqlite3.Connection, now_dt: datetime) -> dict:
    """7. fundamentals：MAX(updated_at) <= 8 天。"""
    if not _has_table(conn, "fundamentals"):
        return {"name": "fundamentals", "status": "missing", "latest": None,
                "expected": "≤8d ago", "detail": "資料表 fundamentals 不存在"}
    row = conn.execute("SELECT MAX(updated_at) FROM fundamentals").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "fundamentals", "status": "missing", "latest": None,
                "expected": "≤8d ago", "detail": "fundamentals 表無資料"}
    try:
        ts = latest.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now_dt - dt
        ok = diff.total_seconds() <= 8 * 86400
        status = "ok" if ok else "stale"
        days_ago = diff.total_seconds() / 86400
        detail = f"距現在 {days_ago:.1f} 天（需<=8d）"
    except Exception as e:
        return {"name": "fundamentals", "status": "stale", "latest": latest,
                "expected": "≤8d ago", "detail": f"時間解析錯誤: {e}"}
    return {"name": "fundamentals", "status": status, "latest": latest,
            "expected": "≤8d ago", "detail": detail}


def check_estimates(conn: sqlite3.Connection, now_dt: datetime) -> dict:
    """8. estimates：analyst_estimates MAX(updated_at) <= 8 天。"""
    if not _has_table(conn, "analyst_estimates"):
        return {"name": "estimates", "status": "missing", "latest": None,
                "expected": "≤8d ago", "detail": "資料表 analyst_estimates 不存在"}
    row = conn.execute("SELECT MAX(updated_at) FROM analyst_estimates").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "estimates", "status": "missing", "latest": None,
                "expected": "≤8d ago", "detail": "analyst_estimates 表無資料"}
    try:
        ts = latest.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now_dt - dt
        ok = diff.total_seconds() <= 8 * 86400
        status = "ok" if ok else "stale"
        days_ago = diff.total_seconds() / 86400
        detail = f"距現在 {days_ago:.1f} 天（需<=8d）"
    except Exception as e:
        return {"name": "estimates", "status": "stale", "latest": latest,
                "expected": "≤8d ago", "detail": f"時間解析錯誤: {e}"}
    return {"name": "estimates", "status": status, "latest": latest,
            "expected": "≤8d ago", "detail": detail}


def check_expert_views(conn: sqlite3.Connection, now_dt: datetime) -> dict:
    """9. expert_views：MAX(fetched_at) <= 8 天。"""
    if not _has_table(conn, "expert_views"):
        return {"name": "expert_views", "status": "missing", "latest": None,
                "expected": "≤8d ago", "detail": "資料表 expert_views 不存在"}
    row = conn.execute("SELECT MAX(fetched_at) FROM expert_views").fetchone()
    latest = row[0] if row else None
    if not latest:
        return {"name": "expert_views", "status": "missing", "latest": None,
                "expected": "≤8d ago", "detail": "expert_views 表無資料"}
    try:
        ts = latest.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now_dt - dt
        ok = diff.total_seconds() <= 8 * 86400
        status = "ok" if ok else "stale"
        days_ago = diff.total_seconds() / 86400
        detail = f"距現在 {days_ago:.1f} 天（需<=8d）"
    except Exception as e:
        return {"name": "expert_views", "status": "stale", "latest": latest,
                "expected": "≤8d ago", "detail": f"時間解析錯誤: {e}"}
    return {"name": "expert_views", "status": status, "latest": latest,
            "expected": "≤8d ago", "detail": detail}


def check_arena_nav(conn: sqlite3.Connection) -> dict:
    """10. arena_nav：agent_nav_daily MAX(date) == prices MAX(date)，且該日 agent 數 == 9。
    detail 需列出缺日清單（prices 中日期 > NAV MAX(date) 的交易日）。
    """
    if not _has_table(conn, "agent_nav_daily"):
        return {"name": "arena_nav", "status": "missing", "latest": None,
                "expected": "prices 最新日 × 9 agents", "detail": "資料表 agent_nav_daily 不存在"}
    if not _has_table(conn, "prices"):
        return {"name": "arena_nav", "status": "missing", "latest": None,
                "expected": "prices 最新日 × 9 agents", "detail": "資料表 prices 不存在"}

    row_nav = conn.execute("SELECT MAX(date) FROM agent_nav_daily").fetchone()
    last_nav_date = row_nav[0] if row_nav else None

    row_price = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    latest_price_date = row_price[0] if row_price else None

    if not latest_price_date:
        return {"name": "arena_nav", "status": "missing", "latest": last_nav_date,
                "expected": "prices 最新日 × 9 agents", "detail": "prices 表無資料"}

    if not last_nav_date:
        return {"name": "arena_nav", "status": "missing", "latest": None,
                "expected": f"{latest_price_date} × 9 agents",
                "detail": "agent_nav_daily 表無資料"}

    # 偵測缺日（參照 catchup.py 78-121 行邏輯）
    missing_dates = []
    if last_nav_date < latest_price_date:
        rows = conn.execute(
            "SELECT DISTINCT date FROM prices WHERE date > ? AND date <= ? ORDER BY date",
            (last_nav_date, latest_price_date),
        ).fetchall()
        missing_dates = [r[0] for r in rows]

    # 確認最新 NAV 日的 agent 數
    agents_row = conn.execute(
        "SELECT COUNT(DISTINCT agent_id) FROM agent_nav_daily WHERE date=?",
        (last_nav_date,),
    ).fetchone()
    agents_cnt = agents_row[0] if agents_row else 0

    date_ok = last_nav_date == latest_price_date
    agents_ok = agents_cnt == 9
    ok = date_ok and agents_ok and not missing_dates
    status = "ok" if ok else "stale"

    detail_parts = [f"NAV最新日={last_nav_date}，prices最新日={latest_price_date}，agents={agents_cnt}"]
    if missing_dates:
        detail_parts.append(f"缺日：{', '.join(missing_dates)}")
    if not agents_ok:
        detail_parts.append(f"最新 NAV 日 agent 數={agents_cnt}（需=9）")

    return {
        "name": "arena_nav",
        "status": status,
        "latest": last_nav_date,
        "expected": f"{latest_price_date} × 9 agents",
        "detail": "；".join(detail_parts),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 執行所有檢查
# ──────────────────────────────────────────────────────────────────────────────

def run_all_checks(db_path: Path) -> tuple[list[dict], str]:
    """回傳 (checks_list, today_str)。"""
    today_str = _taipei_today()
    now_dt = datetime.now(timezone.utc)
    conn = _connect(db_path)
    try:
        checks = [
            check_prices(conn, today_str),
            check_benchmarks(conn, today_str),
            check_signal_history(conn, today_str),
            check_news(conn, now_dt),
            check_stocktwits(conn, now_dt),
            check_tweets(conn, now_dt),
            check_fundamentals(conn, now_dt),
            check_estimates(conn, now_dt),
            check_expert_views(conn, now_dt),
            check_arena_nav(conn),
        ]
    finally:
        conn.close()
    return checks, today_str


# ──────────────────────────────────────────────────────────────────────────────
# 輸出格式
# ──────────────────────────────────────────────────────────────────────────────

STATUS_EMOJI = {"ok": "✅", "stale": "⚠️", "missing": "❌"}
STATUS_ZH = {"ok": "正常", "stale": "過期", "missing": "缺失"}


def print_human_table(checks: list[dict], today_str: str, all_ok: bool) -> None:
    """印 zh-TW 人類可讀表格。"""
    print(f"\n{'='*70}")
    print(f"  Serenity 每日資料健康檢查  as_of={today_str}  {'全數 OK ✅' if all_ok else '有問題 ⚠️'}")
    print(f"{'='*70}")
    col_name = 20
    col_status = 8
    col_latest = 26
    col_expected = 26
    header = (f"{'名稱':<{col_name}} {'狀態':<{col_status}} "
              f"{'最新值':<{col_latest}} {'預期':<{col_expected}} 說明")
    print(header)
    print("-" * 110)
    for c in checks:
        emoji = STATUS_EMOJI.get(c["status"], "?")
        zh_status = STATUS_ZH.get(c["status"], c["status"])
        status_col = f"{emoji}{zh_status}"
        name = c["name"] or ""
        latest = c["latest"] or "NULL"
        expected = c["expected"] or ""
        detail = c["detail"] or ""
        print(f"{name:<{col_name}} {status_col:<{col_status+2}} "
              f"{latest:<{col_latest}} {expected:<{col_expected}} {detail}")
    print(f"{'='*70}\n")


# ──────────────────────────────────────────────────────────────────────────────
# job_runs 記錄
# ──────────────────────────────────────────────────────────────────────────────

def _ensure_job_runs(conn: sqlite3.Connection) -> None:
    conn.execute("""
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
    conn.commit()


def _record_job_run(conn: sqlite3.Connection, job: str, mode: str, cmd: str,
                    started_at: str, finished_at: str, returncode: int,
                    ok: bool, error_tail: str | None) -> None:
    conn.execute(
        "INSERT INTO job_runs(job,mode,cmd,started_at,finished_at,returncode,ok,error_tail) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (job, mode, cmd, started_at, finished_at, returncode, 1 if ok else 0, error_tail),
    )
    conn.commit()


def _append_log(text: str) -> None:
    """安全 append 到 daily_check.log（目錄不存在就建）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass  # log 失敗不中斷主流程


# ──────────────────────────────────────────────────────────────────────────────
# 執行單一步驟
# ──────────────────────────────────────────────────────────────────────────────

def _make_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_step(job: str, cmd_args: list[str], db_path: Path, mode: str,
              dry_run: bool = False) -> dict:
    """執行單一步驟，回傳結果字典。dry_run 時只印計畫不執行。"""
    cmd_str = " ".join(str(a) for a in cmd_args)
    if dry_run:
        print(f"[DRY-RUN] {cmd_str}")
        return {"job": job, "cmd": cmd_str, "returncode": 0, "ok": True,
                "error_tail": None, "dry_run": True}

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = subprocess.run(
            cmd_args,
            cwd=str(ROOT),
            env=_make_env(),
            timeout=1800,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        returncode = result.returncode
        stderr_tail = (result.stderr[-2000:] if result.stderr else None)
    except subprocess.TimeoutExpired as e:
        # 卡死逾時不可讓整個 run/repair 崩掉：記錄為失敗步驟，繼續後面的流程
        returncode = -1
        partial = e.stderr or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        stderr_tail = (f"逾時（>1800s）遭終止。{partial}")[-2000:]
    finished_at = datetime.now(timezone.utc).isoformat()
    ok = returncode == 0

    # 寫 job_runs
    try:
        conn = _connect(db_path)
        _ensure_job_runs(conn)
        _record_job_run(conn, job, mode, cmd_str, started_at, finished_at,
                        returncode, ok, stderr_tail)
        conn.close()
    except Exception:
        pass

    # append log
    log_line = (f"[{finished_at}] {mode} | {job} | rc={returncode} | "
                f"{'OK' if ok else 'FAIL'} | {cmd_str}")
    _append_log(log_line)

    return {
        "job": job,
        "cmd": cmd_str,
        "returncode": returncode,
        "ok": ok,
        "error_tail": stderr_tail,
        "dry_run": False,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 修復計畫
# ──────────────────────────────────────────────────────────────────────────────

REPAIR_ORDER = [
    "prices", "benchmarks", "signal_history", "news", "stocktwits",
    "tweets", "fundamentals", "estimates", "expert_views", "arena_nav",
]


def _repair_steps_for(job: str, db_path: Path) -> list[tuple[str, list]]:
    """回傳 [(job_label, cmd_args), ...] for a given job name."""
    py = sys.executable
    scripts = ROOT / "scripts"
    steps = []
    if job == "prices":
        steps.append((job, [py, str(scripts / "ingest.py"), "prices"]))
    elif job == "benchmarks":
        steps.append((job, [py, str(scripts / "ingest.py"), "benchmarks"]))
    elif job == "signal_history":
        steps.append((job, [py, str(scripts / "server.py"), "--snapshot-once"]))
    elif job == "news":
        steps.append((job, [py, str(scripts / "ingest.py"), "news"]))
    elif job == "stocktwits":
        steps.append((job, [py, str(scripts / "ingest.py"), "stocktwits"]))
    elif job == "tweets":
        steps.append(("tweets_cookies", [py, str(scripts / "crawler.py"), "refresh-cookies"]))
        steps.append(("tweets_fetch", [py, str(scripts / "ingest.py"), "fetch-x",
                                       "--max-pages", "10"]))
    elif job == "fundamentals":
        steps.append((job, [py, str(scripts / "ingest.py"), "fundamentals"]))
    elif job == "estimates":
        steps.append((job, [py, str(scripts / "ingest.py"), "estimates"]))
    elif job == "expert_views":
        steps.append((job, [py, str(scripts / "crawler.py"), "fetch-sources"]))
    elif job == "arena_nav":
        # 先從 DB 取缺日，再對每個缺日跑 agent_arena.py daily --as-of {date}
        steps.append(("arena_nav_backfill", None))  # placeholder，執行時動態展開
    return steps


def _get_arena_missing_dates(db_path: Path) -> list[str]:
    """從 DB 取得 arena_nav 缺日清單。"""
    try:
        conn = _connect(db_path)
        row_nav = conn.execute("SELECT MAX(date) FROM agent_nav_daily").fetchone()
        last_nav = row_nav[0] if row_nav else None
        row_price = conn.execute("SELECT MAX(date) FROM prices").fetchone()
        latest_price = row_price[0] if row_price else None
        conn.close()
        if not last_nav or not latest_price or last_nav >= latest_price:
            return []
        rows = sqlite3.connect(str(db_path)).execute(
            "SELECT DISTINCT date FROM prices WHERE date > ? AND date <= ? ORDER BY date",
            (last_nav, latest_price),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# 指令：check
# ──────────────────────────────────────────────────────────────────────────────

def cmd_check(args) -> int:
    db_path = Path(args.db) if args.db else DEFAULT_DB
    checks, today_str = run_all_checks(db_path)
    all_ok = all(c["status"] == "ok" for c in checks)

    if args.json:
        # --json：stdout 只輸出純 JSON
        payload = {
            "as_of": today_str,
            "ok": all_ok,
            "checks": checks,
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print_human_table(checks, today_str, all_ok)

    return 0 if all_ok else 1


# ──────────────────────────────────────────────────────────────────────────────
# 指令：repair
# ──────────────────────────────────────────────────────────────────────────────

def cmd_repair(args) -> int:
    db_path = Path(args.db) if args.db else DEFAULT_DB
    is_default_db = (db_path.resolve() == DEFAULT_DB.resolve())
    dry_run = getattr(args, "dry_run", False)

    # 安全閥
    if not is_default_db and not dry_run:
        print(f"[daily_check] 安全閥：--db 指向非預設路徑 {db_path}，"
              f"repair 真正執行需使用預設 DB。"
              f"請加 --dry-run 或省略 --db。", file=sys.stderr)
        return 2

    # 先 check
    checks, today_str = run_all_checks(db_path)
    failed_jobs = [c["name"] for c in checks if c["status"] != "ok"]

    if not failed_jobs:
        if not dry_run:
            print("[daily_check] 全部檢查 OK，無需修復。")
        return 0

    if not dry_run:
        print(f"[daily_check] 偵測到非 ok 項目：{failed_jobs}，開始依序修復...")

    step_results = []
    mode = "repair"

    for job in REPAIR_ORDER:
        if job not in failed_jobs:
            continue
        if job == "arena_nav":
            missing_dates = _get_arena_missing_dates(db_path)
            if not missing_dates:
                if not dry_run:
                    print(f"[daily_check] {job}：無缺日，跳過。")
                continue
            for date in missing_dates:
                step_label = f"arena_nav_{date}"
                cmd_args = [sys.executable,
                            str(ROOT / "scripts" / "agent_arena.py"),
                            "daily", "--as-of", date]
                res = _run_step(step_label, cmd_args, db_path, mode, dry_run=dry_run)
                step_results.append(res)
        elif job == "tweets":
            # 兩步：refresh-cookies（失敗不中斷）再 fetch-x
            cookie_args = [sys.executable,
                           str(ROOT / "scripts" / "crawler.py"), "refresh-cookies"]
            cookie_res = _run_step("tweets_cookies", cookie_args, db_path, mode, dry_run=dry_run)
            # cookies 失敗不中斷（K-1 已知問題）
            if not dry_run and not cookie_res["ok"]:
                print(f"[daily_check] tweets cookies 刷新失敗（已知 K-1 問題），繼續...")
            step_results.append(cookie_res)

            fetch_args = [sys.executable,
                          str(ROOT / "scripts" / "ingest.py"),
                          "fetch-x", "--max-pages", "10"]
            fetch_res = _run_step("tweets_fetch", fetch_args, db_path, mode, dry_run=dry_run)
            step_results.append(fetch_res)
        else:
            steps = _repair_steps_for(job, db_path)
            for step_label, cmd_args in steps:
                if cmd_args is None:
                    continue
                res = _run_step(step_label, cmd_args, db_path, mode, dry_run=dry_run)
                step_results.append(res)

    if dry_run:
        return 0

    # 修完再 check
    print("\n[daily_check] 修復完畢，執行終檢...")
    final_checks, final_today = run_all_checks(db_path)
    final_ok = all(c["status"] == "ok" for c in final_checks)

    # 輸出摘要
    print(f"\n{'='*70}")
    print(f"  修復後終檢結果  as_of={final_today}")
    print(f"{'='*70}")
    for res in step_results:
        fixed_mark = "✅" if res["ok"] else "❌"
        print(f"  {fixed_mark} [{res['job']}] rc={res['returncode']} | {res['cmd']}")
        if not res["ok"] and res.get("error_tail"):
            tail = res["error_tail"][-500:]
            print(f"     stderr 尾巴: {tail}")
    print()
    print_human_table(final_checks, final_today, final_ok)

    return 0 if final_ok else 1


# ──────────────────────────────────────────────────────────────────────────────
# 指令：run
# ──────────────────────────────────────────────────────────────────────────────

def cmd_run(args) -> int:
    db_path = Path(args.db) if args.db else DEFAULT_DB
    is_default_db = (db_path.resolve() == DEFAULT_DB.resolve())
    dry_run = getattr(args, "dry_run", False)

    # 安全閥
    if not is_default_db and not dry_run:
        print(f"[daily_check] 安全閥：--db 指向非預設路徑 {db_path}，"
              f"run 真正執行需使用預設 DB。"
              f"請加 --dry-run 或省略 --db。", file=sys.stderr)
        return 2

    py = sys.executable
    scripts = ROOT / "scripts"
    mode = "run"

    # 先 check，決定是否要執行週次任務
    checks, today_str = run_all_checks(db_path)
    check_status = {c["name"]: c["status"] for c in checks}

    # 判斷是否週一
    from datetime import date
    today_date = date.fromisoformat(today_str)
    is_monday = today_date.weekday() == 0  # 0 = Monday

    step_results = []

    # 1. prices
    res = _run_step("prices", [py, str(scripts / "ingest.py"), "prices"],
                    db_path, mode, dry_run=dry_run)
    step_results.append(res)

    # 2. benchmarks
    res = _run_step("benchmarks", [py, str(scripts / "ingest.py"), "benchmarks"],
                    db_path, mode, dry_run=dry_run)
    step_results.append(res)

    # 3. signal_history (snapshot)
    res = _run_step("signal_history", [py, str(scripts / "server.py"), "--snapshot-once"],
                    db_path, mode, dry_run=dry_run)
    step_results.append(res)

    # 4. news
    res = _run_step("news", [py, str(scripts / "ingest.py"), "news"],
                    db_path, mode, dry_run=dry_run)
    step_results.append(res)

    # 5. stocktwits
    res = _run_step("stocktwits", [py, str(scripts / "ingest.py"), "stocktwits"],
                    db_path, mode, dry_run=dry_run)
    step_results.append(res)

    # 6-9. 週次任務：週一或該項非 ok 才排入
    weekly_jobs = [
        ("tweets_cookies", [py, str(scripts / "crawler.py"), "refresh-cookies"]),
        ("tweets_fetch", [py, str(scripts / "ingest.py"), "fetch-x", "--max-pages", "10"]),
        ("fundamentals", [py, str(scripts / "ingest.py"), "fundamentals"]),
        ("estimates", [py, str(scripts / "ingest.py"), "estimates"]),
        ("expert_views", [py, str(scripts / "crawler.py"), "fetch-sources"]),
    ]

    # tweets 對應的 check 名是 "tweets"
    weekly_check_names = {
        "tweets_cookies": "tweets",
        "tweets_fetch": "tweets",
        "fundamentals": "fundamentals",
        "estimates": "estimates",
        "expert_views": "expert_views",
    }

    for job, cmd_args in weekly_jobs:
        check_name = weekly_check_names.get(job, job)
        if is_monday or check_status.get(check_name, "ok") != "ok":
            res = _run_step(job, cmd_args, db_path, mode, dry_run=dry_run)
            step_results.append(res)

    # 10. arena_nav：先補缺日再跑今日
    missing_dates = _get_arena_missing_dates(db_path)
    for date in missing_dates:
        step_label = f"arena_nav_{date}"
        cmd_args = [py, str(scripts / "agent_arena.py"), "daily", "--as-of", date]
        res = _run_step(step_label, cmd_args, db_path, mode, dry_run=dry_run)
        step_results.append(res)
    # 今日 arena（冪等）
    res = _run_step("arena_nav_today", [py, str(scripts / "agent_arena.py"), "daily"],
                    db_path, mode, dry_run=dry_run)
    step_results.append(res)

    if dry_run:
        return 0

    # 跑完 check
    print("\n[daily_check] 完整流程執行完畢，執行終檢...")
    final_checks, final_today = run_all_checks(db_path)
    final_ok = all(c["status"] == "ok" for c in final_checks)

    print(f"\n{'='*70}")
    print(f"  每日流程執行摘要  as_of={final_today}")
    print(f"{'='*70}")
    for res in step_results:
        fixed_mark = "✅" if res["ok"] else "❌"
        print(f"  {fixed_mark} [{res['job']}] rc={res['returncode']} | {res['cmd']}")
        if not res["ok"] and res.get("error_tail"):
            tail = res["error_tail"][-500:]
            print(f"     stderr 尾巴: {tail}")
    print()
    print_human_table(final_checks, final_today, final_ok)

    return 0 if final_ok else 1


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="每日資料健康檢查 + 全流程執行 + 斷點修復",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["check", "run", "repair"],
                        help="子指令：check / run / repair")
    parser.add_argument("--db", default=None,
                        help="DB 路徑（預設：data/serenity.sqlite）")
    parser.add_argument("--json", action="store_true",
                        help="（check 用）stdout 輸出純 JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="列出計畫但不執行；exit 0")
    args = parser.parse_args()

    if args.command == "check":
        sys.exit(cmd_check(args))
    elif args.command == "run":
        sys.exit(cmd_run(args))
    elif args.command == "repair":
        sys.exit(cmd_repair(args))


if __name__ == "__main__":
    main()
