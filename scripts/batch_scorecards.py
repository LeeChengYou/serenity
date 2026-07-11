#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/batch_scorecards.py — 批次記分卡生成工具

功能：
  對 symbol_list 全體，挑「無 scorecard 或 updated_at > --max-age-days」者，
  依序呼叫 serenity.services.scorecard.generate_scorecard(symbol)。

建議排程（太平洋午夜後 = 台北 15:10，使用 J-13 任務槽，僅文件不自動註冊）：
  schtasks /Create /TN "Serenity\\J13_batch_scorecards" /TR "python scripts\\batch_scorecards.py" /SC DAILY /ST 15:10

用法：
  python scripts/batch_scorecards.py [--dry-run] [--limit N] [--pause SEC] [--max-age-days N]

選項：
  --dry-run       每個候選印一行 PLAN <SYMBOL> <原因>，不呼叫 Gemini，exit 0
  --limit N       最多處理 N 個 symbol（預設 20）
  --pause SEC     每個 symbol 間隔秒數（預設 5）
  --max-age-days N  updated_at 超過 N 天視為需重新生成（預設 14）
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 確保 repo root 在 sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from serenity.config import DB_PATH
from serenity.db import db as _db
from scripts.ingest import symbol_list, connect as _ingest_connect


def _ensure_job_runs(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_runs(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job         TEXT NOT NULL,
            mode        TEXT NOT NULL,
            cmd         TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            returncode  INTEGER,
            ok          INTEGER,
            error_tail  TEXT
        )
    """)
    con.commit()


def _record_job_run(con: sqlite3.Connection, symbol: str, ok: bool, error_tail: str, started_at: str) -> None:
    _ensure_job_runs(con)
    finished_at = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO job_runs(job,mode,cmd,started_at,finished_at,returncode,ok,error_tail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            f"scorecard:{symbol}",
            "batch-scorecard",
            f"batch_scorecards.py {symbol}",
            started_at,
            finished_at,
            0 if ok else 1,
            1 if ok else 0,
            error_tail or "",
        ),
    )
    con.commit()


def main():
    ap = argparse.ArgumentParser(description="批次生成記分卡")
    ap.add_argument("--dry-run", action="store_true", help="列出計畫，不呼叫 Gemini")
    ap.add_argument("--limit", type=int, default=20, help="最多處理幾個 symbol（預設 20）")
    ap.add_argument("--pause", type=float, default=5.0, help="每個 symbol 間隔秒數（預設 5）")
    ap.add_argument("--max-age-days", type=int, default=14, help="updated_at 超過幾天視為過期（預設 14）")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[batch_scorecards] 資料庫不存在：{DB_PATH}", file=sys.stderr)
        sys.exit(1)

    # 取得 symbol 清單
    ingest_con = _ingest_connect()
    try:
        symbols = symbol_list(ingest_con)
    finally:
        ingest_con.close()

    if not symbols:
        print("[batch_scorecards] symbol_list 為空，結束。")
        sys.exit(0)

    # 讀取現有記分卡的 updated_at
    sc_con = sqlite3.connect(str(DB_PATH))
    sc_con.row_factory = sqlite3.Row
    try:
        existing = {
            r["symbol"]: r["updated_at"]
            for r in sc_con.execute("SELECT symbol, updated_at FROM scorecards").fetchall()
        }
    except Exception:
        existing = {}
    sc_con.close()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.max_age_days)

    # 篩選候選
    candidates = []
    for sym in symbols:
        if len(candidates) >= args.limit:
            break
        if sym not in existing:
            reason = "無記分卡"
        else:
            updated_str = existing[sym]
            try:
                # 嘗試解析 updated_at（isoformat，可能含或不含時區）
                updated_str_clean = updated_str.rstrip("Z").replace(" ", "T")
                updated_dt = datetime.fromisoformat(updated_str_clean).replace(tzinfo=timezone.utc)
                if updated_dt >= cutoff:
                    continue  # 還新鮮，跳過
                days_old = (datetime.now(timezone.utc) - updated_dt).days
                reason = f"已 {days_old} 天未更新"
            except Exception:
                reason = f"時間解析失敗（{updated_str!r}）"
        candidates.append((sym, reason))

    if not candidates:
        print(f"[batch_scorecards] 所有 symbol 記分卡均在 {args.max_age_days} 天內，無需更新。")
        sys.exit(0)

    if args.dry_run:
        for sym, reason in candidates:
            print(f"PLAN {sym} {reason}")
        sys.exit(0)

    # 實跑
    from serenity.services.scorecard import generate_scorecard
    db_con = sqlite3.connect(str(DB_PATH))

    failed = []
    for i, (sym, reason) in enumerate(candidates):
        started_at = datetime.now(timezone.utc).isoformat()
        print(f"[{i+1}/{len(candidates)}] 生成記分卡：{sym}（{reason}）")
        try:
            result = generate_scorecard(sym)
            if result.get("error"):
                raise RuntimeError(result["error"])
            print(f"  ✓ {sym} 記分卡完成（score={result.get('final_score')}）")
            _record_job_run(db_con, sym, ok=True, error_tail="", started_at=started_at)
        except Exception as exc:
            err_msg = str(exc)[:500]
            print(f"  ✗ {sym} 失敗：{err_msg}", file=sys.stderr)
            _record_job_run(db_con, sym, ok=False, error_tail=err_msg, started_at=started_at)
            failed.append(sym)

        if i < len(candidates) - 1:
            time.sleep(args.pause)

    db_con.close()

    total = len(candidates)
    ok_count = total - len(failed)
    print(f"\n[batch_scorecards] 完成：{ok_count}/{total} 成功，{len(failed)} 失敗")
    if failed:
        print(f"  失敗的 symbol：{', '.join(failed)}", file=sys.stderr)
        if ok_count == 0:
            sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
