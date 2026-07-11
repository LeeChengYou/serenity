# -*- coding: utf-8 -*-
"""
模擬資金池（Fund Pool）— Spec-First 驗收測試（監督者撰寫，具約束力）

執行：python scratch/test_fund_pool.py
原則：
  - 只使用 data/serenity.sqlite 的「備份副本」（sqlite backup API），絕不寫入正式 DB
  - 全程 StubConsultBackend，零 Gemini 呼叫
  - 任何一項 ❌ 即整體 FAIL（exit code 1）

介面契約摘要（完整版見 docs/REQUIREMENTS_FUND_POOL.md §10）：

  scripts/fund_pool.py:
    constants:
      DEFAULT_INITIAL_CASH = 3000.0
      CONSULT_MAX_PARTICIPANTS = 5
      CONSULT_MEMORY_K = 3
      OUTCOME_TRADING_DAYS = 7

    migrate(con) -> None           冪等；建 pools/pool_consults/pool_consult_opinions；
                                   agent_trades 加 fill_mode 欄（已存在則略過）
    create_pool(con, name, initial_cash, created_at=None) -> str
                                   pool_id='pool-<slug>'；seed agents/agent_state/pools；
                                   同名重複 → raise ValueError
    place_order(con, pool_id, side, symbol, *, usd=None, qty=None,
                reason, fill_mode='t1_open', as_of) -> dict
                                   回傳 {"status","trade_id","rejected_reason","fill_price"}
                                   拒單依序：reason 空、池不存在/archived、symbol 不在 prices、
                                   BUY usd>現金、SELL qty>持有
    archive_pool(con, pool_id) -> None
    list_pools(con) -> list[dict]

    class ConsultBackend(ABC): opine(agent_id, prompt)->dict; synthesize(prompt)->str
    class StubConsultBackend(ConsultBackend):
      __init__(opine_map=None, synthesize_text="stub summary")
      .opine_prompts: list[tuple[agent_id, prompt]]
      .synthesize_prompts: list[str]
    class GeminiConsultBackend(ConsultBackend)

    run_consult(con, pool_id, question, symbol, participants, as_of, backend) -> int
    backfill_outcomes(con, as_of) -> None  冪等

  serenity/services/pool_views.py:
    pool_list_payload(con) -> dict
    pool_detail_payload(con, pool_id) -> dict
    pool_consults_payload(con, pool_id) -> dict

  scripts/server.py:
    arena_leaderboard_payload(con, month) -> dict  （列加 kind 欄）
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

RESULTS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    status = "  PASS " if ok else "! FAIL "
    print(status, name, ("" if ok else f"-- {detail}"))
    return ok


def approx(a, b, rel=1e-4):
    """數值近似比較（相對誤差 rel 以內）"""
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= rel * max(1.0, abs(float(b)))


def finish():
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print()
    print("=" * 70)
    print(f"Fund Pool Acceptance Test — {n_pass} passed / {n_fail} failed")
    print("=" * 70)
    sys.exit(0 if n_fail == 0 else 1)


def main():
    print("=" * 70)
    print("Fund Pool Spec-First Acceptance Test")
    print("=" * 70)

    # ── P0: import fund_pool ──────────────────────────────────────────────
    print("\n[P0] import fund_pool & constants")
    try:
        import fund_pool as fp
    except Exception as exc:
        # 功能尚未實作：標記所有後續測項為 FAIL 並正常退出
        check("import scripts/fund_pool.py", False, repr(exc))
        _mark_all_unrun()
        return finish()

    check("DEFAULT_INITIAL_CASH == 3000.0",
          getattr(fp, "DEFAULT_INITIAL_CASH", None) == 3000.0)
    check("CONSULT_MAX_PARTICIPANTS == 5",
          getattr(fp, "CONSULT_MAX_PARTICIPANTS", None) == 5)
    check("CONSULT_MEMORY_K == 3",
          getattr(fp, "CONSULT_MEMORY_K", None) == 3)
    check("OUTCOME_TRADING_DAYS == 7",
          getattr(fp, "OUTCOME_TRADING_DAYS", None) == 7)

    # ── import arena ─────────────────────────────────────────────────────
    print("\n[P0b] import agent_arena (需要 SLIPPAGE_BPS 等常數)")
    try:
        import agent_arena as arena
    except Exception as exc:
        check("import scripts/agent_arena.py", False, repr(exc))
        return finish()
    check("import agent_arena.py OK", True)

    SLIPPAGE_BPS = getattr(arena, "SLIPPAGE_BPS", 5)

    # ── setup: sandbox copy of live DB ───────────────────────────────────
    print("\n[setup] sandbox DB copy (live DB untouched)")
    tmpdir = Path(tempfile.mkdtemp(prefix="fund_pool_test_"))
    test_db = tmpdir / "test.sqlite"
    src = sqlite3.connect(ROOT / "data" / "serenity.sqlite")
    dst = sqlite3.connect(test_db)
    src.backup(dst)
    src.close()
    dst.close()

    # 確保正式 DB 路徑不在 tmpdir 之內
    check("test DB 在 tempdir（非正式 DB 路徑）",
          str(test_db).startswith(str(tmpdir)))

    con = arena.connect(test_db)
    # 先跑 arena.migrate 確保基礎表存在
    arena.migrate(con)

    # ── P1: migrate 冪等 ─────────────────────────────────────────────────
    print("\n[P1] migrate 冪等（跑兩次無錯；三張新表 + fill_mode 欄）")
    try:
        fp.migrate(con)
        fp.migrate(con)
        ok_migrate = True
    except Exception as exc:
        ok_migrate = False
        check("migrate 兩次無例外", False, repr(exc))

    if ok_migrate:
        check("migrate 兩次無例外", True)

    tables = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'").fetchall()}
    check("table pools exists",              "pools" in tables)
    check("table pool_consults exists",      "pool_consults" in tables)
    check("table pool_consult_opinions exists", "pool_consult_opinions" in tables)

    # fill_mode 欄存在於 agent_trades
    at_cols = {r[1] for r in con.execute("PRAGMA table_info(agent_trades)").fetchall()}
    check("agent_trades.fill_mode 欄存在", "fill_mode" in at_cols)

    # ── P2: create_pool ──────────────────────────────────────────────────
    print("\n[P2] create_pool — agents/agent_state/pools seed 正確；同名 ValueError；pool_id 格式")
    try:
        pool_id = fp.create_pool(con, "Test Alpha", 3000.0, created_at="2025-06-01")
    except Exception as exc:
        check("create_pool 無例外", False, repr(exc))
        return finish()
    check("create_pool 無例外", True)
    check("pool_id 格式 'pool-<slug>'",
          isinstance(pool_id, str) and pool_id.startswith("pool-"),
          f"got {pool_id!r}")

    # agents 表有一列 backend='human'
    agent_row = con.execute(
        "SELECT id, domain, style_seed, backend, status FROM agents WHERE id=?",
        (pool_id,)).fetchone()
    check("agents 表有 pool 列",          agent_row is not None)
    check("agents.backend = 'human'",     agent_row and agent_row["backend"] == "human")
    check("agents.domain = 'human'",      agent_row and agent_row["domain"] == "human")
    check("agents.style_seed = 'human'",  agent_row and agent_row["style_seed"] == "human")
    check("agents.status = 'active'",     agent_row and agent_row["status"] == "active")

    # agent_state seed
    state_row = con.execute(
        "SELECT cash, nav FROM agent_state WHERE agent_id=?", (pool_id,)).fetchone()
    check("agent_state 有記錄",    state_row is not None)
    check("agent_state.cash == 3000", state_row and approx(state_row["cash"], 3000.0))
    check("agent_state.nav == 3000",  state_row and approx(state_row["nav"], 3000.0))

    # pools 表 seed
    pool_row = con.execute(
        "SELECT agent_id, display_name, initial_cash, status FROM pools WHERE agent_id=?",
        (pool_id,)).fetchone()
    check("pools 表有記錄",               pool_row is not None)
    check("pools.initial_cash == 3000",   pool_row and approx(pool_row["initial_cash"], 3000.0))
    check("pools.status = 'active'",      pool_row and pool_row["status"] == "active")

    # hwm == initial_cash
    hwm_row = con.execute(
        "SELECT hwm FROM agents WHERE id=?", (pool_id,)).fetchone()
    check("agents.hwm == initial_cash",   hwm_row and approx(hwm_row["hwm"], 3000.0))

    # 同名重複 → ValueError
    try:
        fp.create_pool(con, "Test Alpha", 3000.0)
        check("同名重複 raise ValueError", False, "未拋出例外")
    except ValueError:
        check("同名重複 raise ValueError", True)
    except Exception as exc:
        check("同名重複 raise ValueError", False, f"錯誤類型: {type(exc).__name__}: {exc}")

    # ── P3: t1_open 買單 ─────────────────────────────────────────────────
    print("\n[P3] t1_open 買單 → run_daily → 成交價 = 次日開盤 × (1+slip)；現金/持倉/NAV 正確")
    # 使用真實 DB 資料：TSLA as_of=2025-06-02，次日=2025-06-03 open=346.60
    AS_OF_T1  = "2025-06-02"
    NEXT_DAY  = "2025-06-03"
    SYM_T1    = "TSLA"
    # 從 DB 副本現算期望值（不寫死）
    next_open_row = con.execute(
        "SELECT open FROM prices WHERE symbol=? AND date=?",
        (SYM_T1, NEXT_DAY)).fetchone()
    expected_open = next_open_row["open"] if next_open_row else None
    check(f"prices 有 {SYM_T1} {NEXT_DAY} open",
          expected_open is not None, f"got {expected_open}")

    # 建第二個資金池供 t1_open 測試
    pool_t1 = fp.create_pool(con, "T1 Pool", 3000.0, created_at=AS_OF_T1)

    # 下 BUY 單（usd=500）
    buy_usd = 500.0
    try:
        order_result = fp.place_order(
            con, pool_t1, "BUY", SYM_T1,
            usd=buy_usd, reason="test t1_open buy", fill_mode="t1_open", as_of=AS_OF_T1)
    except Exception as exc:
        check("place_order t1_open BUY 無例外", False, repr(exc))
        order_result = {}
    else:
        check("place_order t1_open BUY 無例外", True)

    check("t1_open 回傳 status=pending",
          order_result.get("status") == "pending",
          f"got {order_result.get('status')!r}")
    check("t1_open 回傳 trade_id (int)",
          isinstance(order_result.get("trade_id"), int),
          f"got {order_result.get('trade_id')!r}")

    # 跑 run_daily 到次日（只對 pool_t1）
    try:
        arena.run_daily(con, NEXT_DAY, arena.StubBackend(),
                        only_agents=[pool_t1])
        check("run_daily(次日) 無例外", True)
    except Exception as exc:
        check("run_daily(次日) 無例外", False, repr(exc))

    # 驗證成交
    trade_id = order_result.get("trade_id")
    if trade_id is not None:
        trade = con.execute(
            "SELECT status, exec_date, price, qty, cash_after, fill_mode "
            "FROM agent_trades WHERE id=?", (trade_id,)).fetchone()
        check("trade 已 filled",        trade and trade["status"] == "filled",
              f"status={trade['status'] if trade else None}")
        check("exec_date == NEXT_DAY",   trade and trade["exec_date"] == NEXT_DAY)
        check("fill_mode == 't1_open'",  trade and trade["fill_mode"] == "t1_open")

        if trade and trade["status"] == "filled" and expected_open is not None:
            slip_factor = 1.0 + SLIPPAGE_BPS / 10000.0
            expected_fill = expected_open * slip_factor
            expected_qty  = buy_usd / expected_fill
            check("fill_price ≈ next_open × (1+slip)",
                  approx(trade["price"], expected_fill, rel=1e-3),
                  f"got {trade['price']:.4f}, expected {expected_fill:.4f}")
            # 現金 = 3000 - qty*fill_price
            actual_cost = trade["qty"] * trade["price"]
            expected_cash_after = 3000.0 - actual_cost
            check("cash_after 正確（3000 - 成交金額）",
                  approx(trade["cash_after"], expected_cash_after, rel=1e-3),
                  f"got {trade['cash_after']:.4f}, expected {expected_cash_after:.4f}")

        # 持倉
        pos = con.execute(
            "SELECT qty, avg_cost FROM agent_positions WHERE agent_id=? AND symbol=?",
            (pool_t1, SYM_T1)).fetchone()
        check("持倉有 TSLA",             pos is not None)
        if pos and trade:
            check("持倉 qty > 0",        pos["qty"] > 0)

        # NAV 有記錄
        nav_row = con.execute(
            "SELECT nav FROM agent_nav_daily WHERE agent_id=? AND date=?",
            (pool_t1, NEXT_DAY)).fetchone()
        check("agent_nav_daily 有記錄（次日）", nav_row is not None)
    else:
        for name in ["trade 已 filled", "exec_date == NEXT_DAY",
                     "fill_mode == 't1_open'", "fill_price ≈ next_open × (1+slip)",
                     "cash_after 正確", "持倉有 TSLA", "持倉 qty > 0",
                     "agent_nav_daily 有記錄（次日）"]:
            check(name, False, "trade_id 為 None，跳過")

    # ── P4: latest_close 單 ──────────────────────────────────────────────
    print("\n[P4] latest_close 單 — 立即 filled；fill_mode 標注；fill_price = 最新 close × (1±slip)")
    AS_OF_LC = "2025-06-05"
    SYM_LC   = "NVDA"
    # 從 DB 副本現算最新 close <= AS_OF_LC
    lc_row = con.execute(
        "SELECT date, close FROM prices WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT 1",
        (SYM_LC, AS_OF_LC)).fetchone()
    check(f"prices 有 {SYM_LC} close <= {AS_OF_LC}",
          lc_row is not None, f"got {lc_row}")

    pool_lc = fp.create_pool(con, "LC Pool", 5000.0, created_at=AS_OF_LC)
    try:
        lc_result = fp.place_order(
            con, pool_lc, "BUY", SYM_LC,
            usd=300.0, reason="latest_close test", fill_mode="latest_close", as_of=AS_OF_LC)
    except Exception as exc:
        check("place_order latest_close 無例外", False, repr(exc))
        lc_result = {}
    else:
        check("place_order latest_close 無例外", True)

    check("latest_close status=filled",
          lc_result.get("status") == "filled",
          f"got {lc_result.get('status')!r}")
    check("latest_close fill_price 非 None",
          lc_result.get("fill_price") is not None)

    if lc_row and lc_result.get("fill_price") is not None:
        slip_buy = 1.0 + SLIPPAGE_BPS / 10000.0
        expected_lc_fill = lc_row["close"] * slip_buy
        check("fill_price ≈ latest_close × (1+slip)",
              approx(lc_result["fill_price"], expected_lc_fill, rel=1e-3),
              f"got {lc_result['fill_price']:.4f}, expected {expected_lc_fill:.4f}")

    # 驗證 DB 內 fill_mode='latest_close' 且 exec_date = 該 close 日期
    lc_tid = lc_result.get("trade_id")
    if lc_tid is not None:
        lc_trade = con.execute(
            "SELECT fill_mode, exec_date, status FROM agent_trades WHERE id=?",
            (lc_tid,)).fetchone()
        check("fill_mode='latest_close' in DB",
              lc_trade and lc_trade["fill_mode"] == "latest_close",
              f"got {lc_trade['fill_mode'] if lc_trade else None}")
        check("status='filled' in DB",
              lc_trade and lc_trade["status"] == "filled")
        if lc_row:
            check("exec_date = 最新 close 日期",
                  lc_trade and lc_trade["exec_date"] == lc_row["date"],
                  f"got {lc_trade['exec_date'] if lc_trade else None}, expected {lc_row['date']}")

    # ── P5: 五種拒單 ─────────────────────────────────────────────────────
    print("\n[P5] 五種拒單（依序）：reason 空、池 archived、symbol 不在 prices、BUY 現金不足、SELL 賣超")
    pool_reject = fp.create_pool(con, "Reject Test", 500.0, created_at="2025-06-01")

    # 1) reason 空 → rejected
    try:
        r1 = fp.place_order(con, pool_reject, "BUY", "TSLA",
                            usd=100.0, reason="", fill_mode="t1_open", as_of="2025-06-02")
    except Exception as exc:
        r1 = {"status": "error", "rejected_reason": repr(exc)}
    check("拒單 reason 空 → status=rejected",
          r1.get("status") == "rejected",
          f"got {r1.get('status')!r}")
    check("拒單 reason 空 → rejected_reason 非空",
          bool(r1.get("rejected_reason")))

    # 2) 池 archived → rejected（先 archive 一個池）
    pool_archived = fp.create_pool(con, "Archived Pool", 1000.0, created_at="2025-06-01")
    fp.archive_pool(con, pool_archived)
    try:
        r2 = fp.place_order(con, pool_archived, "BUY", "TSLA",
                            usd=100.0, reason="should fail", fill_mode="t1_open", as_of="2025-06-02")
    except Exception as exc:
        r2 = {"status": "error", "rejected_reason": repr(exc)}
    check("拒單 池 archived → status=rejected",
          r2.get("status") == "rejected",
          f"got {r2.get('status')!r}")
    check("拒單 池 archived → rejected_reason 非空",
          bool(r2.get("rejected_reason")))

    # 3) symbol 不在 prices → rejected
    try:
        r3 = fp.place_order(con, pool_reject, "BUY", "NOTEXIST99",
                            usd=100.0, reason="bad symbol", fill_mode="t1_open", as_of="2025-06-02")
    except Exception as exc:
        r3 = {"status": "error", "rejected_reason": repr(exc)}
    check("拒單 symbol 不在 prices → status=rejected",
          r3.get("status") == "rejected",
          f"got {r3.get('status')!r}")
    check("拒單 symbol 不在 prices → rejected_reason 非空",
          bool(r3.get("rejected_reason")))

    # 4) BUY 現金不足 → rejected（pool_reject 初始 500，下超額單）
    try:
        r4 = fp.place_order(con, pool_reject, "BUY", "TSLA",
                            usd=9999.0, reason="too expensive", fill_mode="t1_open", as_of="2025-06-02")
    except Exception as exc:
        r4 = {"status": "error", "rejected_reason": repr(exc)}
    check("拒單 BUY 現金不足 → status=rejected",
          r4.get("status") == "rejected",
          f"got {r4.get('status')!r}")
    check("拒單 BUY 現金不足 → rejected_reason 非空",
          bool(r4.get("rejected_reason")))

    # 5) SELL 賣超 → rejected（pool_reject 無 TSLA 持倉）
    try:
        r5 = fp.place_order(con, pool_reject, "SELL", "TSLA",
                            qty=100.0, reason="oversell", fill_mode="t1_open", as_of="2025-06-02")
    except Exception as exc:
        r5 = {"status": "error", "rejected_reason": repr(exc)}
    check("拒單 SELL 賣超 → status=rejected",
          r5.get("status") == "rejected",
          f"got {r5.get('status')!r}")
    check("拒單 SELL 賣超 → rejected_reason 非空",
          bool(r5.get("rejected_reason")))

    # ── P6: run_daily 對 human 池 — 不產生 briefing/決策；NAV 有記錄；冪等 ──
    print("\n[P6] run_daily 對 human 池：無新 agent_trades/agent_memory；NAV 有記錄；跑兩次冪等")
    pool_daily = fp.create_pool(con, "Daily Test", 3000.0, created_at="2025-06-02")
    DAILY_AS_OF = "2025-06-03"

    # 記錄 agent_trades 與 agent_memory 的現有數量
    trades_before = con.execute(
        "SELECT COUNT(*) FROM agent_trades WHERE agent_id=?", (pool_daily,)).fetchone()[0]
    memory_before = con.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE agent_id=?", (pool_daily,)).fetchone()[0]

    try:
        arena.run_daily(con, DAILY_AS_OF, arena.StubBackend(),
                        only_agents=[pool_daily])
        check("run_daily(human pool) 第一次無例外", True)
    except Exception as exc:
        check("run_daily(human pool) 第一次無例外", False, repr(exc))

    trades_after = con.execute(
        "SELECT COUNT(*) FROM agent_trades WHERE agent_id=?", (pool_daily,)).fetchone()[0]
    memory_after = con.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE agent_id=?", (pool_daily,)).fetchone()[0]

    check("run_daily 不新增 agent_trades 列（無下單）",
          trades_after == trades_before,
          f"before={trades_before}, after={trades_after}")
    check("run_daily 不新增 agent_memory 列",
          memory_after == memory_before,
          f"before={memory_before}, after={memory_after}")

    nav_row_d = con.execute(
        "SELECT nav FROM agent_nav_daily WHERE agent_id=? AND date=?",
        (pool_daily, DAILY_AS_OF)).fetchone()
    check("run_daily 產生 agent_nav_daily 記錄",
          nav_row_d is not None, f"date={DAILY_AS_OF}")

    # 跑第二次（冪等）
    try:
        arena.run_daily(con, DAILY_AS_OF, arena.StubBackend(),
                        only_agents=[pool_daily])
        check("run_daily(human pool) 第二次無例外（冪等）", True)
    except Exception as exc:
        check("run_daily(human pool) 第二次無例外（冪等）", False, repr(exc))

    nav_count = con.execute(
        "SELECT COUNT(*) FROM agent_nav_daily WHERE agent_id=? AND date=?",
        (pool_daily, DAILY_AS_OF)).fetchone()[0]
    check("run_daily 冪等：agent_nav_daily 同日不重複",
          nav_count == 1, f"count={nav_count}")

    # ── P7: run_monthly 不碰 human 池 ────────────────────────────────────
    print("\n[P7] run_monthly：human 池不被淘汰/relaunch/reflect")
    pool_monthly = fp.create_pool(con, "Monthly Test", 3000.0, created_at="2025-06-01")
    # 先記錄 status 與 relaunches
    status_before = con.execute(
        "SELECT status, relaunches FROM agents WHERE id=?", (pool_monthly,)).fetchone()
    mem_before_monthly = con.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE agent_id=?", (pool_monthly,)).fetchone()[0]

    try:
        arena.run_monthly(con, "2025-06", arena.StubBackend())
        check("run_monthly 無例外", True)
    except Exception as exc:
        check("run_monthly 無例外", False, repr(exc))

    status_after = con.execute(
        "SELECT status, relaunches FROM agents WHERE id=?", (pool_monthly,)).fetchone()
    mem_after_monthly = con.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE agent_id=?", (pool_monthly,)).fetchone()[0]

    check("run_monthly 不改 human 池 status",
          status_after and status_after["status"] == "active",
          f"got {status_after['status'] if status_after else None}")
    check("run_monthly 不增 human 池 relaunches",
          status_before and status_after and
          status_after["relaunches"] == status_before["relaunches"],
          f"before={status_before['relaunches'] if status_before else None}, "
          f"after={status_after['relaunches'] if status_after else None}")
    check("run_monthly 不產生 human 池 reflection（agent_memory）",
          mem_after_monthly == mem_before_monthly,
          f"before={mem_before_monthly}, after={mem_after_monthly}")

    # ── P8: archive_pool 後下單被拒；list_pools 仍列出 ──────────────────
    print("\n[P8] archive_pool → 下單被拒；list_pools 仍列出封存池")
    pool_arch2 = fp.create_pool(con, "Archive Test 2", 2000.0, created_at="2025-06-01")
    try:
        fp.archive_pool(con, pool_arch2)
        check("archive_pool 無例外", True)
    except Exception as exc:
        check("archive_pool 無例外", False, repr(exc))

    # agents.status='retired'
    arch_row = con.execute(
        "SELECT status FROM agents WHERE id=?", (pool_arch2,)).fetchone()
    check("archive → agents.status='retired'",
          arch_row and arch_row["status"] == "retired",
          f"got {arch_row['status'] if arch_row else None}")
    # pools.status='archived'
    arch_pool = con.execute(
        "SELECT status FROM pools WHERE agent_id=?", (pool_arch2,)).fetchone()
    check("archive → pools.status='archived'",
          arch_pool and arch_pool["status"] == "archived",
          f"got {arch_pool['status'] if arch_pool else None}")

    # 下單被拒
    try:
        r_arch = fp.place_order(con, pool_arch2, "BUY", "TSLA",
                                usd=100.0, reason="after archive", fill_mode="t1_open",
                                as_of="2025-06-02")
    except Exception as exc:
        r_arch = {"status": "error", "rejected_reason": repr(exc)}
    check("封存池下單 → rejected",
          r_arch.get("status") == "rejected",
          f"got {r_arch.get('status')!r}")

    # list_pools 仍含該池
    try:
        all_pools = fp.list_pools(con)
        arch_ids = [p.get("pool_id") or p.get("agent_id") for p in all_pools]
        check("list_pools 含封存池",
              pool_arch2 in arch_ids,
              f"pool_arch2={pool_arch2!r}, ids={arch_ids}")
    except Exception as exc:
        check("list_pools 含封存池", False, repr(exc))

    # ── P9: 會診 StubConsultBackend ──────────────────────────────────────
    print("\n[P9] run_consult — pool_consults/pool_consult_opinions 落庫；主席 summary 正確")
    pool_consult = fp.create_pool(con, "Consult Pool", 3000.0, created_at="2025-06-01")
    AS_OF_CONSULT = "2025-06-05"

    stub_cb = fp.StubConsultBackend(
        opine_map=None,
        synthesize_text="stub summary text")

    PARTICIPANTS = ["semis-momentum", "semis-dip", "semis-catalyst"]
    SYM_CONSULT = "NVDA"
    QUESTION = "Should we buy NVDA given current valuation?"

    try:
        consult_id = fp.run_consult(
            con, pool_consult, QUESTION, SYM_CONSULT,
            PARTICIPANTS, AS_OF_CONSULT, stub_cb)
        check("run_consult 無例外", True)
        check("run_consult 回傳 int consult_id",
              isinstance(consult_id, int), f"got {type(consult_id).__name__}")
    except Exception as exc:
        check("run_consult 無例外", False, repr(exc))
        check("run_consult 回傳 int consult_id", False, "例外發生，跳過")
        consult_id = None

    if consult_id is not None:
        consult_row = con.execute(
            "SELECT pool_id, symbol, question, summary, as_of FROM pool_consults WHERE id=?",
            (consult_id,)).fetchone()
        check("pool_consults 有記錄",       consult_row is not None)
        check("pool_consults.summary 非空",
              consult_row and consult_row["summary"] is not None and len(consult_row["summary"]) > 0,
              f"summary={consult_row['summary'] if consult_row else None!r}")
        check("pool_consults.pool_id 正確",
              consult_row and consult_row["pool_id"] == pool_consult)
        check("pool_consults.symbol 正確",
              consult_row and consult_row["symbol"] == SYM_CONSULT)

        opinions = con.execute(
            "SELECT agent_id, stance, confidence, opinion FROM pool_consult_opinions WHERE consult_id=?",
            (consult_id,)).fetchall()
        check("pool_consult_opinions 有 N 列（每參與者一列）",
              len(opinions) == len(PARTICIPANTS),
              f"expected {len(PARTICIPANTS)}, got {len(opinions)}")
        for op in opinions:
            check(f"opinion {op['agent_id']} stance 非空",
                  op["stance"] in ("support", "oppose", "neutral", "absent"),
                  f"got {op['stance']!r}")

        # opine 的 prompt 內含 question 與 briefing 相關內容
        check("StubConsultBackend.opine_prompts 記錄了各次 prompt",
              len(stub_cb.opine_prompts) == len(PARTICIPANTS),
              f"expected {len(PARTICIPANTS)}, got {len(stub_cb.opine_prompts)}")
        for agent_id_p, prompt_p in stub_cb.opine_prompts:
            check(f"opine prompt[{agent_id_p}] 含 question",
                  QUESTION in prompt_p,
                  f"prompt 前 200 chars: {prompt_p[:200]!r}")
        check("synthesize_prompts 記錄了綜合輪 prompt",
              len(stub_cb.synthesize_prompts) >= 1)

    # ── P10: 會診記憶 ────────────────────────────────────────────────────
    print("\n[P10] 會診記憶：第二次同 symbol 會診 prompt 含第一次 summary")
    if consult_id is not None:
        stub_cb2 = fp.StubConsultBackend(synthesize_text="second stub summary")
        try:
            consult_id2 = fp.run_consult(
                con, pool_consult, "NVDA again — still buy?", SYM_CONSULT,
                PARTICIPANTS, AS_OF_CONSULT, stub_cb2)
            check("第二次 run_consult 無例外", True)
        except Exception as exc:
            check("第二次 run_consult 無例外", False, repr(exc))
            consult_id2 = None

        if consult_id2 is not None:
            first_summary = "stub summary text"
            # 至少有一個 opine_prompt 含第一次 summary
            found_in_opine = any(
                first_summary in prompt_p
                for _, prompt_p in stub_cb2.opine_prompts)
            check("第二次 opine_prompts 含第一次 summary（記憶注入）",
                  found_in_opine,
                  f"first_summary={first_summary!r}, "
                  f"prompts_preview={[p[:150] for _, p in stub_cb2.opine_prompts]}")
            found_in_synth = any(
                first_summary in p for p in stub_cb2.synthesize_prompts)
            check("第二次 synthesize_prompts 含第一次 summary（記憶注入）",
                  found_in_synth)
    else:
        check("第二次 run_consult 無例外", False, "consult_id 為 None，跳過")
        check("第二次 opine_prompts 含第一次 summary", False, "跳過")
        check("第二次 synthesize_prompts 含第一次 summary", False, "跳過")

    # ── P11: 降級 ────────────────────────────────────────────────────────
    print("\n[P11] 降級：1 個 agent raise → stance=absent；全部 raise → summary=NULL")
    pool_degrade = fp.create_pool(con, "Degrade Pool", 3000.0, created_at="2025-06-01")

    # 部分失敗：第一個 agent raise，其餘正常
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated 429")

    partial_opine_map = {
        PARTICIPANTS[0]: _raise,
    }
    stub_partial = fp.StubConsultBackend(
        opine_map=partial_opine_map, synthesize_text="partial summary")
    try:
        cid_partial = fp.run_consult(
            con, pool_degrade, "partial fail test", SYM_CONSULT,
            PARTICIPANTS, AS_OF_CONSULT, stub_partial)
        check("部分降級 run_consult 無例外", True)
    except Exception as exc:
        check("部分降級 run_consult 無例外", False, repr(exc))
        cid_partial = None

    if cid_partial is not None:
        absent_ops = con.execute(
            "SELECT agent_id, stance FROM pool_consult_opinions "
            "WHERE consult_id=? AND stance='absent'", (cid_partial,)).fetchall()
        check("部分降級：失敗 agent stance='absent'",
              len(absent_ops) == 1 and absent_ops[0]["agent_id"] == PARTICIPANTS[0],
              f"absent_ops={[(o['agent_id'], o['stance']) for o in absent_ops]}")
        summary_row = con.execute(
            "SELECT summary FROM pool_consults WHERE id=?", (cid_partial,)).fetchone()
        check("部分降級：summary 仍產出（非 NULL）",
              summary_row and summary_row["summary"] is not None)

    # 全部失敗 → summary = NULL
    all_fail_map = {p: _raise for p in PARTICIPANTS}
    stub_allfail = fp.StubConsultBackend(
        opine_map=all_fail_map, synthesize_text="should not appear")
    try:
        cid_allfail = fp.run_consult(
            con, pool_degrade, "all fail test", SYM_CONSULT,
            PARTICIPANTS, AS_OF_CONSULT, stub_allfail)
        check("全部降級 run_consult 無例外", True)
    except Exception as exc:
        check("全部降級 run_consult 無例外", False, repr(exc))
        cid_allfail = None

    if cid_allfail is not None:
        all_absent = con.execute(
            "SELECT COUNT(*) FROM pool_consult_opinions "
            "WHERE consult_id=? AND stance='absent'", (cid_allfail,)).fetchone()[0]
        check("全部降級：所有 stance='absent'",
              all_absent == len(PARTICIPANTS),
              f"absent={all_absent}, total participants={len(PARTICIPANTS)}")
        null_summary = con.execute(
            "SELECT summary FROM pool_consults WHERE id=?", (cid_allfail,)).fetchone()
        check("全部降級：summary = NULL",
              null_summary and null_summary["summary"] is None,
              f"summary={null_summary['summary'] if null_summary else '(no row)'!r}")

    # ── P12: backfill_outcomes ────────────────────────────────────────────
    print("\n[P12] backfill_outcomes：outcome_7d 從 prices 計算；缺價維持 NULL；冪等")
    pool_bf = fp.create_pool(con, "Backfill Pool", 3000.0, created_at="2025-05-09")

    # 選一個有足夠後續價格的 symbol 與日期
    # 使用 TSLA，as_of=2025-05-09，確認其後至少 7 個交易日存在
    BF_SYMBOL = "TSLA"
    BF_AS_OF  = "2025-05-09"
    # 從 DB 現算 7 個交易日後的 close 與基礎 close
    bf_dates = con.execute(
        "SELECT date, close FROM prices WHERE symbol=? AND date > ? ORDER BY date",
        (BF_SYMBOL, BF_AS_OF)).fetchall()
    bf_base_row = con.execute(
        "SELECT close FROM prices WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT 1",
        (BF_SYMBOL, BF_AS_OF)).fetchone()
    check("backfill 測試用 symbol 有足夠後續交易日（>=7）",
          len(bf_dates) >= 7, f"got {len(bf_dates)} days after {BF_AS_OF}")
    check("backfill 測試用 symbol 有基礎 close",
          bf_base_row is not None and bf_base_row["close"] is not None)

    # 構造一筆舊會診（直接 INSERT，模擬已存在但 outcome_7d IS NULL 的記錄）
    con.execute("""
        INSERT INTO pool_consults (pool_id, symbol, as_of, question, summary,
                                   followed, outcome_7d, created_at)
        VALUES (?, ?, ?, ?, 'test summary', NULL, NULL, ?)
    """, (pool_bf, BF_SYMBOL, BF_AS_OF,
          "backfill test question", BF_AS_OF + "T00:00:00"))
    con.commit()
    bf_consult_id = con.execute(
        "SELECT id FROM pool_consults WHERE pool_id=? AND as_of=? ORDER BY id DESC LIMIT 1",
        (pool_bf, BF_AS_OF)).fetchone()[0]

    # 計算期望 outcome_7d（從 DB 副本現算）
    if len(bf_dates) >= 7 and bf_base_row and bf_base_row["close"]:
        day7_close   = bf_dates[6]["close"]   # 第 7 個交易日
        base_close   = bf_base_row["close"]
        expected_o7d = day7_close / base_close - 1.0
    else:
        expected_o7d = None

    # 跑 backfill
    BF_RUN_AS_OF = "2025-06-15"
    try:
        fp.backfill_outcomes(con, BF_RUN_AS_OF)
        check("backfill_outcomes 無例外", True)
    except Exception as exc:
        check("backfill_outcomes 無例外", False, repr(exc))

    bf_row = con.execute(
        "SELECT outcome_7d FROM pool_consults WHERE id=?", (bf_consult_id,)).fetchone()
    check("backfill_outcomes 填入 outcome_7d",
          bf_row and bf_row["outcome_7d"] is not None,
          f"outcome_7d={bf_row['outcome_7d'] if bf_row else None}")
    if expected_o7d is not None and bf_row and bf_row["outcome_7d"] is not None:
        check("outcome_7d 值正確（第 7 交易日 close / 基礎 close - 1）",
              approx(bf_row["outcome_7d"], expected_o7d, rel=1e-4),
              f"got {bf_row['outcome_7d']:.6f}, expected {expected_o7d:.6f}")

    # 構造不足 7 日的記錄（as_of 在最近日期附近）
    RECENT_AS_OF = "2026-07-05"
    # 確認 TSLA 在 RECENT_AS_OF 後的交易日 < 7
    recent_dates = con.execute(
        "SELECT date FROM prices WHERE symbol=? AND date > ? ORDER BY date",
        (BF_SYMBOL, RECENT_AS_OF)).fetchall()
    if len(recent_dates) < 7:
        con.execute("""
            INSERT INTO pool_consults (pool_id, symbol, as_of, question, summary,
                                       followed, outcome_7d, created_at)
            VALUES (?, ?, ?, ?, 'recent summary', NULL, NULL, ?)
        """, (pool_bf, BF_SYMBOL, RECENT_AS_OF,
              "recent backfill test", RECENT_AS_OF + "T00:00:00"))
        con.commit()
        recent_consult_id = con.execute(
            "SELECT id FROM pool_consults WHERE pool_id=? AND as_of=? ORDER BY id DESC LIMIT 1",
            (pool_bf, RECENT_AS_OF)).fetchone()[0]
        fp.backfill_outcomes(con, BF_RUN_AS_OF)
        recent_row = con.execute(
            "SELECT outcome_7d FROM pool_consults WHERE id=?", (recent_consult_id,)).fetchone()
        check("不足 7 日的會診 outcome_7d 維持 NULL",
              recent_row and recent_row["outcome_7d"] is None,
              f"outcome_7d={recent_row['outcome_7d'] if recent_row else None}")
    else:
        check("不足 7 日測試：RECENT_AS_OF 之後交易日不足 7",
              False,
              f"got {len(recent_dates)} days after {RECENT_AS_OF}；請調整日期")

    # backfill 冪等（跑第二次，outcome_7d 不變）
    try:
        fp.backfill_outcomes(con, BF_RUN_AS_OF)
        check("backfill_outcomes 冪等（第二次無例外）", True)
    except Exception as exc:
        check("backfill_outcomes 冪等（第二次無例外）", False, repr(exc))

    bf_row2 = con.execute(
        "SELECT outcome_7d FROM pool_consults WHERE id=?", (bf_consult_id,)).fetchone()
    if expected_o7d is not None and bf_row2 and bf_row2["outcome_7d"] is not None:
        check("backfill_outcomes 冪等：outcome_7d 不變",
              approx(bf_row2["outcome_7d"], expected_o7d, rel=1e-4),
              f"got {bf_row2['outcome_7d']:.6f}, expected {expected_o7d:.6f}")

    # ── P13: pool_views ──────────────────────────────────────────────────
    print("\n[P13] pool_views — pool_list_payload / pool_detail_payload / "
          "pool_consults_payload / arena_leaderboard_payload")
    try:
        from serenity.services import pool_views as pv
        check("import serenity.services.pool_views", True)
    except Exception as exc:
        check("import serenity.services.pool_views", False, repr(exc))
        pv = None

    if pv is not None:
        # pool_list_payload
        try:
            pl = pv.pool_list_payload(con)
            check("pool_list_payload 無例外", True)
        except Exception as exc:
            check("pool_list_payload 無例外", False, repr(exc))
            pl = None

        if pl is not None:
            check("pool_list_payload 回傳 dict 含 'pools' key", "pools" in pl)
            if "pools" in pl and len(pl["pools"]) > 0:
                sample = pl["pools"][0]
                for field in ["pool_id", "name", "initial_cash", "status",
                               "nav", "cash", "total_return_pct", "mdd",
                               "pending_orders", "created_at"]:
                    check(f"pool_list_payload.pools[0] 含欄位 {field}",
                          field in sample, f"keys={list(sample.keys())}")

        # pool_detail_payload（使用 pool_t1）
        try:
            pd = pv.pool_detail_payload(con, pool_t1)
            check("pool_detail_payload 無例外", True)
        except Exception as exc:
            check("pool_detail_payload 無例外", False, repr(exc))
            pd = None

        if pd is not None:
            for field in ["positions", "nav_series", "trades"]:
                check(f"pool_detail_payload 含 '{field}' key",
                      field in pd, f"keys={list(pd.keys())}")
            if "trades" in pd and len(pd["trades"]) > 0:
                t_sample = pd["trades"][0]
                for field in ["fill_mode", "reason", "status"]:
                    check(f"trades 含欄位 {field}",
                          field in t_sample, f"keys={list(t_sample.keys())}")

        # pool_consults_payload
        try:
            pc = pv.pool_consults_payload(con, pool_consult)
            check("pool_consults_payload 無例外", True)
        except Exception as exc:
            check("pool_consults_payload 無例外", False, repr(exc))
            pc = None

        if pc is not None:
            check("pool_consults_payload 回傳 dict 含 'consults' key", "consults" in pc)

    # arena_leaderboard_payload 含 kind 欄且 human 池在列
    try:
        import server
        check("import server 無例外", True)
    except Exception as exc:
        check("import server 無例外", False, repr(exc))
        server = None

    if server is not None:
        # 先跑 run_daily 對 pool_t1 確保有 NAV
        try:
            lb = server.arena_leaderboard_payload(con, "2025-06")
            check("arena_leaderboard_payload 無例外", True)
        except Exception as exc:
            check("arena_leaderboard_payload 無例外", False, repr(exc))
            lb = None

        if lb is not None and "rows" in lb:
            # 檢查是否有 kind 欄（契約要求新增）
            if len(lb["rows"]) > 0:
                check("arena_leaderboard rows 含 'kind' 欄",
                      "kind" in lb["rows"][0],
                      f"keys={list(lb['rows'][0].keys())}")
            # 檢查 human 池出現在排行榜（需有 NAV 資料的 human 池）
            human_rows = [r for r in lb["rows"]
                          if r.get("kind") == "human" or
                          r.get("agent_id", "").startswith("pool-")]
            check("排行榜含 human 池記錄",
                  len(human_rows) > 0,
                  f"human_rows count={len(human_rows)}")

    # ── P14: 審查退回必修案例（M1/M2/M3/W1，2026-07-12 fresh-context 審查）──
    print("\n[P14] 輸入防護：initial_cash<=0、slug 碰撞、負數/零下單、會診人數上限")

    # M3: create_pool initial_cash <= 0 → ValueError
    for bad_cash in (0.0, -500.0):
        try:
            fp.create_pool(con, f"Bad Cash {bad_cash}", bad_cash, created_at="2025-06-01")
            check(f"create_pool initial_cash={bad_cash} → ValueError", False, "未 raise")
        except ValueError:
            check(f"create_pool initial_cash={bad_cash} → ValueError", True)
        except Exception as exc:
            check(f"create_pool initial_cash={bad_cash} → ValueError", False,
                  f"raise 了非 ValueError: {exc!r}")

    # M2: slug 碰撞（不同 display_name、相同 slug）→ 第二次必須 raise，第一池不被動
    pool_col = fp.create_pool(con, "Collide Pool", 3000.0, created_at="2025-06-01")
    col_cash_before = con.execute(
        "SELECT cash FROM agent_state WHERE agent_id=? ORDER BY month DESC LIMIT 1",
        (pool_col,)).fetchone()["cash"]
    try:
        pid2 = fp.create_pool(con, "collide  pool", 9999.0, created_at="2025-06-01")
        check("slug 碰撞 create_pool → ValueError", False, f"未 raise，回傳 {pid2!r}")
    except ValueError:
        check("slug 碰撞 create_pool → ValueError", True)
    except Exception as exc:
        check("slug 碰撞 create_pool → ValueError", False, f"raise 了非 ValueError: {exc!r}")
    col_cash_after = con.execute(
        "SELECT cash FROM agent_state WHERE agent_id=? ORDER BY month DESC LIMIT 1",
        (pool_col,)).fetchone()["cash"]
    check("slug 碰撞後原池 cash 未被動",
          approx(col_cash_after, col_cash_before, rel=1e-9),
          f"before={col_cash_before}, after={col_cash_after}")
    col_pool_rows = con.execute(
        "SELECT COUNT(*) FROM pools WHERE agent_id=?", (pool_col,)).fetchone()[0]
    check("slug 碰撞後 pools 僅 1 列", col_pool_rows == 1, f"got {col_pool_rows}")

    # M1: BUY usd<=0 / SELL qty<=0 → rejected（兩種 fill_mode 都擋），資金與持倉不得變動
    pool_guard = fp.create_pool(con, "Guard Pool", 3000.0, created_at="2025-06-01")
    AS_OF_G = "2025-06-05"
    for mode in ("t1_open", "latest_close"):
        for bad_usd in (0.0, -300.0):
            try:
                rg = fp.place_order(con, pool_guard, "BUY", "NVDA",
                                    usd=bad_usd, reason="guard test",
                                    fill_mode=mode, as_of=AS_OF_G)
            except Exception as exc:
                rg = {"status": "error", "rejected_reason": repr(exc)}
            check(f"BUY usd={bad_usd} ({mode}) → rejected",
                  rg.get("status") == "rejected", f"got {rg.get('status')!r}")
    for bad_qty in (0.0, -1.0):
        try:
            rg = fp.place_order(con, pool_guard, "SELL", "NVDA",
                                qty=bad_qty, reason="guard test",
                                fill_mode="latest_close", as_of=AS_OF_G)
        except Exception as exc:
            rg = {"status": "error", "rejected_reason": repr(exc)}
        check(f"SELL qty={bad_qty} → rejected",
              rg.get("status") == "rejected", f"got {rg.get('status')!r}")
    g_cash = con.execute(
        "SELECT cash FROM agent_state WHERE agent_id=? ORDER BY month DESC LIMIT 1",
        (pool_guard,)).fetchone()["cash"]
    check("負數/零下單後現金仍 3000（沒有憑空生現金）",
          approx(g_cash, 3000.0, rel=1e-9), f"got {g_cash}")
    g_pos = con.execute(
        "SELECT COUNT(*) FROM agent_positions WHERE agent_id=?", (pool_guard,)).fetchone()[0]
    check("負數/零下單後無持倉（沒有負持倉/放空）", g_pos == 0, f"got {g_pos}")
    g_pending = con.execute(
        "SELECT COUNT(*) FROM agent_trades WHERE agent_id=? AND status='pending'",
        (pool_guard,)).fetchone()[0]
    check("負數/零下單後無 pending 單", g_pending == 0, f"got {g_pending}")

    # W1: run_consult participants 數量 0 或 >CONSULT_MAX_PARTICIPANTS → ValueError，且不呼叫 opine
    ai_agents = [r["id"] for r in con.execute(
        "SELECT id FROM agents WHERE backend != 'human' ORDER BY id").fetchall()]
    stub_guard = fp.StubConsultBackend(synthesize_text="guard summary")
    for plist, label in (([], "0 人"), (ai_agents[:7], "7 人")):
        try:
            fp.run_consult(con, pool_guard, "guard question", "NVDA",
                           plist, AS_OF_G, stub_guard)
            check(f"run_consult participants {label} → ValueError", False, "未 raise")
        except ValueError:
            check(f"run_consult participants {label} → ValueError", True)
        except Exception as exc:
            check(f"run_consult participants {label} → ValueError", False,
                  f"raise 了非 ValueError: {exc!r}")
    check("超限會診未呼叫任何 opine（省配額）",
          len(stub_guard.opine_prompts) == 0,
          f"opine called {len(stub_guard.opine_prompts)} 次")

    # ── 結尾 ─────────────────────────────────────────────────────────────
    con.close()
    return finish()


# 標記所有後續預期測項為 FAIL（import 失敗時使用）
EXPECTED_TEST_NAMES = [
    # P0
    "DEFAULT_INITIAL_CASH == 3000.0",
    "CONSULT_MAX_PARTICIPANTS == 5",
    "CONSULT_MEMORY_K == 3",
    "OUTCOME_TRADING_DAYS == 7",
    # P1
    "migrate 兩次無例外",
    "table pools exists", "table pool_consults exists", "table pool_consult_opinions exists",
    "agent_trades.fill_mode 欄存在",
    # P2
    "create_pool 無例外", "pool_id 格式 'pool-<slug>'",
    "agents 表有 pool 列", "agents.backend = 'human'", "agents.domain = 'human'",
    "agents.style_seed = 'human'", "agents.status = 'active'",
    "agent_state 有記錄", "agent_state.cash == 3000", "agent_state.nav == 3000",
    "pools 表有記錄", "pools.initial_cash == 3000", "pools.status = 'active'",
    "agents.hwm == initial_cash", "同名重複 raise ValueError",
    # P3
    "place_order t1_open BUY 無例外", "t1_open 回傳 status=pending",
    "t1_open 回傳 trade_id (int)", "run_daily(次日) 無例外",
    "trade 已 filled", "exec_date == NEXT_DAY", "fill_mode == 't1_open'",
    "fill_price ≈ next_open × (1+slip)", "cash_after 正確（3000 - 成交金額）",
    "持倉有 TSLA", "持倉 qty > 0", "agent_nav_daily 有記錄（次日）",
    # P4
    "place_order latest_close 無例外", "latest_close status=filled",
    "latest_close fill_price 非 None", "fill_price ≈ latest_close × (1+slip)",
    "fill_mode='latest_close' in DB", "status='filled' in DB", "exec_date = 最新 close 日期",
    # P5 (五種拒單)
    "拒單 reason 空 → status=rejected", "拒單 reason 空 → rejected_reason 非空",
    "拒單 池 archived → status=rejected", "拒單 池 archived → rejected_reason 非空",
    "拒單 symbol 不在 prices → status=rejected", "拒單 symbol 不在 prices → rejected_reason 非空",
    "拒單 BUY 現金不足 → status=rejected", "拒單 BUY 現金不足 → rejected_reason 非空",
    "拒單 SELL 賣超 → status=rejected", "拒單 SELL 賣超 → rejected_reason 非空",
    # P6
    "run_daily(human pool) 第一次無例外", "run_daily 不新增 agent_trades 列（無下單）",
    "run_daily 不新增 agent_memory 列", "run_daily 產生 agent_nav_daily 記錄",
    "run_daily(human pool) 第二次無例外（冪等）", "run_daily 冪等：agent_nav_daily 同日不重複",
    # P7
    "run_monthly 無例外", "run_monthly 不改 human 池 status",
    "run_monthly 不增 human 池 relaunches", "run_monthly 不產生 human 池 reflection（agent_memory）",
    # P8
    "archive_pool 無例外", "archive → agents.status='retired'", "archive → pools.status='archived'",
    "封存池下單 → rejected", "list_pools 含封存池",
    # P9
    "run_consult 無例外", "run_consult 回傳 int consult_id",
    "pool_consults 有記錄", "pool_consults.summary 非空",
    "pool_consults.pool_id 正確", "pool_consults.symbol 正確",
    "pool_consult_opinions 有 N 列（每參與者一列）",
    "StubConsultBackend.opine_prompts 記錄了各次 prompt",
    "synthesize_prompts 記錄了綜合輪 prompt",
    # P10
    "第二次 run_consult 無例外", "第二次 opine_prompts 含第一次 summary（記憶注入）",
    "第二次 synthesize_prompts 含第一次 summary（記憶注入）",
    # P11
    "部分降級 run_consult 無例外", "部分降級：失敗 agent stance='absent'",
    "部分降級：summary 仍產出（非 NULL）",
    "全部降級 run_consult 無例外", "全部降級：所有 stance='absent'", "全部降級：summary = NULL",
    # P12
    "backfill_outcomes 無例外", "backfill_outcomes 填入 outcome_7d",
    "outcome_7d 值正確（第 7 交易日 close / 基礎 close - 1）",
    "不足 7 日的會診 outcome_7d 維持 NULL",
    "backfill_outcomes 冪等（第二次無例外）", "backfill_outcomes 冪等：outcome_7d 不變",
    # P13
    "import serenity.services.pool_views",
    "pool_list_payload 無例外", "pool_list_payload 回傳 dict 含 'pools' key",
    "pool_detail_payload 無例外", "pool_consults_payload 無例外",
    "arena_leaderboard_payload 無例外",
    "arena_leaderboard rows 含 'kind' 欄", "排行榜含 human 池記錄",
]


def _mark_all_unrun():
    """import 失敗時，把尚未執行的測項全部標記為 FAIL（輸出完整測項清單）"""
    already = {name for name, _, _ in RESULTS}
    for name in EXPECTED_TEST_NAMES:
        if name not in already:
            check(name, False, "模組未實作，跳過")


if __name__ == "__main__":
    main()
