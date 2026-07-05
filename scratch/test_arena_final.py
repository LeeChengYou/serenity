# -*- coding: utf-8 -*-
"""
V6 AI 經理人競技場 — Final Acceptance Test（監督者撰寫，具約束力）

執行：python scratch/test_arena_final.py
原則：
  - 只使用 data/serenity.sqlite 的「備份副本」，絕不寫入正式 DB
  - 全程 StubBackend，零 Gemini 呼叫
  - 任何一項 ❌ 即整體 FAIL（exit code 1）

本測試同時定義 scripts/agent_arena.py 的公開介面契約（與
docs/REQUIREMENTS_V6.md 併讀；兩者矛盾時以本測試為準）：

  constants: INIT_CAPITAL=3000.0, MAX_POS_PCT=0.40,
             MAX_TRADES_PER_DAY=3, SLIPPAGE_BPS=5,
             RELAUNCH_DD=0.40, RELAUNCH_FLOOR=1500.0,
             DOMAINS: dict[str, list[str]]（semis/robotics/ai_cloud）
  connect(db_path) -> sqlite3.Connection (row_factory=Row)
  migrate(con)                       冪等建表（含 agent_memory 共 7 表）
  seed_agents(con, data_dir=None)    冪等植入 9 agents + 策略卡/format_prefs 檔案
  build_briefing(con, domain, as_of, agent_id, data_dir=None) -> str
  class AgentBackend(ABC):
      decide(agent_id, briefing, strategy) -> dict
      reflect(agent_id, dossier) -> dict
  class StubBackend(AgentBackend):
      __init__(decide_map=None, reflect_map=None)   # dict[agent_id, dict]
  run_daily(con, as_of, backend, only_agents=None, data_dir=None)
  run_monthly(con, month, backend, data_dir=None)

  訂單語義：
   - actions 依序評估，第 4 筆起自動拒單（exceeds daily limit）
   - BUY {symbol, usd, reason} / SELL {symbol, pct, reason}
   - 拒單條件：不在領域股票池、現金不足、單股 >40% NAV、無持倉可賣
   - 接受的單 status='pending'，於下一次 run_daily 以當日開盤價
     ±5bps 滑價成交（買加賣減），status='filled'，exec_date=成交日
   - 空 actions 也是合法決策；冪等：同 (agent, as_of) 已有
     agent_nav_daily 列即跳過該 agent

  server.py 需新增（函式接受外部 con，供本測試直接呼叫）：
   arena_leaderboard_payload(con, month) -> {"month","rows":[...]}
   arena_nav_payload(con, month)         -> {"series":{...},"benchmark":{...}}
   arena_trades_payload(con, agent_id, month) -> {"trades":[...]}
   arena_reflections_payload(con, month) -> {"rows":[...]}
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

RESULTS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(("  PASS " if ok else "! FAIL "), name, ("" if ok else f"-- {detail}"))
    return ok


def approx(a, b, rel=1e-4):
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= rel * max(1.0, abs(float(b)))


def main():
    print("=" * 70)
    print("V6 Arena Final Acceptance Test")
    print("=" * 70)

    # ── P0: import & constants ──────────────────────────────────────────
    print("\n[P0] module import & constants")
    try:
        import agent_arena as arena
    except Exception as exc:
        check("import scripts/agent_arena.py", False, repr(exc))
        return finish()

    check("INIT_CAPITAL == 3000", getattr(arena, "INIT_CAPITAL", None) == 3000.0)
    check("MAX_POS_PCT == 0.40", getattr(arena, "MAX_POS_PCT", None) == 0.40)
    check("MAX_TRADES_PER_DAY == 3", getattr(arena, "MAX_TRADES_PER_DAY", None) == 3)
    check("SLIPPAGE_BPS == 5", getattr(arena, "SLIPPAGE_BPS", None) == 5)
    check("RELAUNCH_FLOOR == 1500", getattr(arena, "RELAUNCH_FLOOR", None) == 1500.0)
    domains = getattr(arena, "DOMAINS", {})
    check("DOMAINS has semis/robotics/ai_cloud",
          set(domains.keys()) == {"semis", "robotics", "ai_cloud"},
          f"got {sorted(domains.keys())}")

    # ── setup: sandbox copy of live DB ─────────────────────────────────
    print("\n[setup] sandbox DB copy (sqlite backup API, live DB untouched)")
    tmpdir = Path(tempfile.mkdtemp(prefix="arena_test_"))
    test_db = tmpdir / "test.sqlite"
    src = sqlite3.connect(ROOT / "data" / "serenity.sqlite")
    dst = sqlite3.connect(test_db)
    src.backup(dst)
    src.close()
    dst.close()
    data_dir = tmpdir  # agents/ and briefings/ live under tmp during test

    con = arena.connect(test_db)
    check("connect() returns Row factory",
          con.row_factory == sqlite3.Row or isinstance(
              con.execute("select 1 as x").fetchone(), sqlite3.Row))

    # ── P1: migrate + seed, idempotent ──────────────────────────────────
    print("\n[P1] migrate + seed (run twice each; must be idempotent)")
    arena.migrate(con)
    arena.migrate(con)
    tables = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'").fetchall()}
    for t in ["agents", "agent_state", "agent_positions", "agent_trades",
              "agent_monthly", "agent_nav_daily", "agent_memory"]:
        check(f"table {t} exists", t in tables)

    arena.seed_agents(con, data_dir=data_dir)
    arena.seed_agents(con, data_dir=data_dir)
    agents = con.execute("select id, domain, style_seed from agents order by id").fetchall()
    check("exactly 9 agents", len(agents) == 9, f"got {len(agents)}")
    by_domain = {}
    for a in agents:
        by_domain.setdefault(a["domain"], []).append(a["style_seed"])
    for d in ["semis", "robotics", "ai_cloud"]:
        check(f"domain {d}: 3 agents, styles momentum/dip/catalyst",
              sorted(by_domain.get(d, [])) == ["catalyst", "dip", "momentum"],
              f"got {by_domain.get(d)}")
    aid = "semis-momentum"
    check("strategy.md exists", (data_dir / "agents" / aid / "strategy.md").exists())
    prefs_file = data_dir / "agents" / aid / "format_prefs.json"
    check("format_prefs.json exists", prefs_file.exists())
    if prefs_file.exists():
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        check("format_prefs has news_count", "news_count" in prefs)

    # ── trading days for the test window ────────────────────────────────
    days = [r[0] for r in con.execute(
        "select distinct date from prices where symbol='NVDA' "
        "and date >= '2026-06-01' order by date limit 4").fetchall()]
    if len(days) < 4:
        check("4 trading days available in test window", False, f"got {days}")
        return finish()
    T1, T2, T3, T4 = days
    print(f"  test days: {T1} {T2} {T3} {T4}")

    # ── P2: briefing ─────────────────────────────────────────────────────
    print("\n[P2] briefing generation (compact + no look-ahead)")
    brief = arena.build_briefing(con, "semis", T1, aid, data_dir=data_dir)
    check("briefing non-empty str", isinstance(brief, str) and len(brief) > 200)
    for token in ["BRIEF", "PRICES", "NVDA", "PORTFOLIO"]:
        check(f"briefing contains '{token}'", token in brief)
    check("briefing compact (< 12000 chars ≈ 3K tokens)",
          len(brief) < 12000, f"len={len(brief)}")
    # no look-ahead: a news title published AFTER T1 must not appear
    future_news = con.execute(
        "select title from news where published_at > ? order by published_at desc limit 3",
        (T1 + "T23:59:59",)).fetchall()
    leaked = [r["title"] for r in future_news if r["title"] and r["title"][:30] in brief]
    check("no future news leaked into briefing", not leaked, f"leaked: {leaked[:1]}")

    # ── P3: decision validation (engine-enforced rules) ─────────────────
    print("\n[P3] decision day T1 — engine rules (anti all-in, pool, daily cap)")
    stub = arena.StubBackend(decide_map={aid: {
        "actions": [
            {"side": "BUY", "symbol": "NVDA", "usd": 2900, "reason": "all-in 測試（應拒）"},
            {"side": "BUY", "symbol": "ZZZFAKE", "usd": 100, "reason": "池外股票（應拒）"},
            {"side": "BUY", "symbol": "NVDA", "usd": 1000, "reason": "動能首倉（應成）"},
            {"side": "BUY", "symbol": "TSM", "usd": 500, "reason": "第 4 筆（應拒：日限）"},
        ],
        "watch": ["AVGO"],
        "memory_note": "測試記憶",
    }})
    arena.run_daily(con, T1, stub, only_agents=[aid], data_dir=data_dir)

    rows = con.execute(
        "select * from agent_trades where agent_id=? and decided_date=? order by id",
        (aid, T1)).fetchall()
    check("4 actions all recorded", len(rows) == 4, f"got {len(rows)}")
    if len(rows) == 4:
        check("all-in 2900/3000 rejected (40% cap)", rows[0]["status"] == "rejected",
              rows[0]["status"])
        check("out-of-pool symbol rejected", rows[1]["status"] == "rejected")
        check("valid 1000 BUY pending", rows[2]["status"] == "pending",
              rows[2]["status"])
        check("4th action rejected (daily cap 3)", rows[3]["status"] == "rejected")
        check("rejected rows carry rejected_reason",
              all(r["rejected_reason"] for r in rows if r["status"] == "rejected"))
        check("reasons stored verbatim", rows[2]["reason"] == "動能首倉（應成）")
        check("briefing snapshot path recorded & file exists",
              rows[2]["briefing_path"] and Path(rows[2]["briefing_path"]).exists(),
              str(rows[2]["briefing_path"]))
    nav1 = con.execute(
        "select nav, cash from agent_nav_daily where agent_id=? and date=?",
        (aid, T1)).fetchone()
    check("NAV row for T1 (still all cash)", nav1 is not None and approx(nav1["nav"], 3000))

    # ── P4: fill at T2 open ± slippage ───────────────────────────────────
    print("\n[P4] fill day T2 — T+1 open price with 5bps slippage")
    arena.run_daily(con, T2, arena.StubBackend(), only_agents=[aid], data_dir=data_dir)
    t = con.execute(
        "select * from agent_trades where agent_id=? and decided_date=? and status='filled'",
        (aid, T1)).fetchone()
    open2 = con.execute("select open, close from prices where symbol='NVDA' and date=?",
                        (T2,)).fetchone()
    check("pending order filled on T2", t is not None and t["exec_date"] == T2)
    if t and open2 and open2["open"]:
        want_px = open2["open"] * (1 + 5 / 10000.0)
        check("fill price = open*(1+5bps)", approx(t["price"], want_px),
              f"got {t['price']} want {want_px}")
        check("qty = usd / fill_px", approx(t["qty"], 1000.0 / want_px),
              f"got {t['qty']}")
        check("cash_after ≈ 2000", approx(t["cash_after"], 2000.0),
              f"got {t['cash_after']}")
    pos = con.execute(
        "select * from agent_positions where agent_id=? and symbol='NVDA'", (aid,)).fetchone()
    check("position row created", pos is not None and pos["qty"] > 0)
    nav2 = con.execute(
        "select nav, cash from agent_nav_daily where agent_id=? and date=?",
        (aid, T2)).fetchone()
    if nav2 and pos and open2:
        want_nav = nav2["cash"] + pos["qty"] * open2["close"]
        check("NAV(T2) = cash + qty*close (mark-to-market, 含未實現)",
              approx(nav2["nav"], want_nav), f"got {nav2['nav']} want {want_nav}")

    # idempotency
    n_before = con.execute("select count(*) from agent_trades where agent_id=?",
                           (aid,)).fetchone()[0]
    arena.run_daily(con, T2, arena.StubBackend(decide_map={aid: {
        "actions": [{"side": "BUY", "symbol": "AMD", "usd": 300, "reason": "重跑不應執行"}],
        "watch": [], "memory_note": ""}}), only_agents=[aid], data_dir=data_dir)
    n_after = con.execute("select count(*) from agent_trades where agent_id=?",
                          (aid,)).fetchone()[0]
    check("same-day rerun is a no-op (idempotent)", n_before == n_after,
          f"{n_before} -> {n_after}")

    # ── P5: SELL flow + more rejections ──────────────────────────────────
    print("\n[P5] SELL 50% + no-position / insufficient-cash rejections")
    dip = "semis-dip"
    arena.run_daily(con, T3, arena.StubBackend(decide_map={
        aid: {"actions": [{"side": "SELL", "symbol": "NVDA", "pct": 50,
                           "reason": "獲利了結一半"}], "watch": [], "memory_note": ""},
        dip: {"actions": [
            {"side": "SELL", "symbol": "AVGO", "pct": 100, "reason": "無持倉（應拒）"},
            {"side": "BUY", "symbol": "NVDA", "usd": 5000, "reason": "現金不足（應拒）"},
        ], "watch": [], "memory_note": ""},
    }), only_agents=[aid, dip], data_dir=data_dir)

    dip_rows = con.execute(
        "select status from agent_trades where agent_id=? and decided_date=? order by id",
        (dip, T3)).fetchall()
    check("SELL without position rejected",
          len(dip_rows) >= 1 and dip_rows[0]["status"] == "rejected")
    check("BUY beyond cash rejected",
          len(dip_rows) >= 2 and dip_rows[1]["status"] == "rejected")

    qty_before = pos["qty"] if pos else 0
    arena.run_daily(con, T4, arena.StubBackend(), only_agents=[aid, dip], data_dir=data_dir)
    sold = con.execute(
        "select * from agent_trades where agent_id=? and decided_date=? and side='SELL' "
        "and status='filled'", (aid, T3)).fetchone()
    open4 = con.execute("select open from prices where symbol='NVDA' and date=?",
                        (T4,)).fetchone()
    check("SELL filled on T4", sold is not None and sold["exec_date"] == T4)
    if sold and open4 and open4["open"]:
        want_px = open4["open"] * (1 - 5 / 10000.0)
        check("sell fill = open*(1-5bps)", approx(sold["price"], want_px))
        check("sold qty = 50% of position", approx(sold["qty"], qty_before / 2))
    pos_after = con.execute(
        "select qty from agent_positions where agent_id=? and symbol='NVDA'",
        (aid,)).fetchone()
    check("remaining position ≈ half", pos_after is not None and
          approx(pos_after["qty"], qty_before / 2))

    # ── P6: monthly settlement + reflection + relaunch rule ─────────────
    print("\n[P6] monthly close: settle / reflect / strategy rewrite / relaunch")
    month = T1[:7]
    cat = "semis-catalyst"
    # surgically fabricate a blown-up agent to exercise the relaunch clause
    con.execute("insert or replace into agent_nav_daily(agent_id,date,nav,cash) "
                "values (?,?,?,?)", (cat, T1, 3000.0, 3000.0))
    con.execute("insert or replace into agent_nav_daily(agent_id,date,nav,cash) "
                "values (?,?,?,?)", (cat, T4, 1200.0, 1200.0))
    con.commit()

    new_card = "# semis-momentum 策略卡 v2\n- 反思後新規則：財報前 3 日不建新倉\n"
    reflect_payload = {
        "public_letter": "本月最有效：突破追蹤。最痛：無。",
        "reflection_md": "檢討：建倉節奏偏快。",
        "strategy_md": new_card,
    }
    arena.run_monthly(con, month, arena.StubBackend(reflect_map={
        aid: reflect_payload, dip: reflect_payload, cat: reflect_payload,
    }), data_dir=data_dir)

    m = con.execute("select * from agent_monthly where agent_id=? and month=?",
                    (aid, month)).fetchone()
    check("agent_monthly row written", m is not None)
    if m:
        navs = con.execute(
            "select nav from agent_nav_daily where agent_id=? and date like ? "
            "order by date", (aid, month + "%")).fetchall()
        want_ret = (navs[-1]["nav"] / navs[0]["nav"] - 1) * 100
        check("ret_pct = nav_end/nav_start - 1 (%)", approx(m["ret_pct"], want_ret, 1e-3),
              f"got {m['ret_pct']} want {want_ret}")
        check("public_letter archived", (m["public_letter"] or "").startswith("本月最有效"))
        check("strategy_before/after archived",
              m["strategy_before"] and m["strategy_after"] == new_card)
    card_now = (data_dir / "agents" / aid / "strategy.md").read_text(encoding="utf-8")
    check("strategy.md rewritten by reflection", card_now == new_card)
    # positions must SURVIVE month end (持倉延續制)
    pos_carry = con.execute(
        "select qty from agent_positions where agent_id=? and symbol='NVDA'",
        (aid,)).fetchone()
    check("positions carry across month end (no forced liquidation)",
          pos_carry is not None and pos_carry["qty"] > 0)
    # relaunch: NAV 1200 < floor 1500
    cat_row = con.execute("select relaunches from agents where id=?", (cat,)).fetchone()
    check("blown-up agent relaunched (relaunches=1)",
          cat_row is not None and cat_row["relaunches"] == 1,
          f"got {cat_row['relaunches'] if cat_row else None}")
    cat_state = con.execute(
        "select cash from agent_state where agent_id=? order by month desc limit 1",
        (cat,)).fetchone()
    check("relaunched agent reset to $3000 cash",
          cat_state is not None and approx(cat_state["cash"], 3000.0),
          f"got {cat_state['cash'] if cat_state else None}")

    # ── P7: server API payload contracts ────────────────────────────────
    print("\n[P7] server.py arena payload contracts")
    try:
        import server as srv
        lb = srv.arena_leaderboard_payload(con, month)
        check("leaderboard: month + rows", lb.get("month") == month and
              isinstance(lb.get("rows"), list) and len(lb["rows"]) >= 1)
        if lb.get("rows"):
            r0 = lb["rows"][0]
            check("leaderboard row fields",
                  all(k in r0 for k in ["agent_id", "domain", "ret_pct",
                                        "n_trades", "rank_domain", "rank_overall"]),
                  f"got {sorted(r0.keys())}")
        nv = srv.arena_nav_payload(con, month)
        check("nav: series contains agent", aid in nv.get("series", {}))
        check("nav: benchmark key present", "benchmark" in nv)
        tr = srv.arena_trades_payload(con, aid, month)
        check("trades payload non-empty with reason",
              tr.get("trades") and all("reason" in x for x in tr["trades"]))
        rf = srv.arena_reflections_payload(con, month)
        check("reflections rows include public_letter",
              rf.get("rows") and any(x.get("public_letter") for x in rf["rows"]))
    except Exception as exc:
        check("server arena payloads importable/callable", False, repr(exc))

    # ── P8: frontend static presence ─────────────────────────────────────
    print("\n[P8] frontend arena page (static checks)")
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
    check("index.html: arena nav button", 'data-page="arena"' in html)
    check("index.html: arenaView section", 'id="arenaView"' in html)
    check("index.html: 模擬資金 disclaimer", "模擬資金" in html)
    check("app.js: loads /api/arena/", "/api/arena/" in js)

    # ── P9: CLI smoke ─────────────────────────────────────────────────────
    print("\n[P9] CLI smoke")
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "agent_arena.py"),
                        "--help"], capture_output=True, text=True, timeout=60)
    check("agent_arena.py --help exits 0", r.returncode == 0, r.stderr[:200])
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "agent_arena.py"),
                        "status", "--db", str(test_db)],
                       capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    check("status --db <sandbox> exits 0", r.returncode == 0, (r.stderr or "")[:200])

    con.close()
    return finish()


def finish():
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_fail = len(RESULTS) - n_pass
    print("\n" + "=" * 70)
    print(f"RESULT: {n_pass} passed, {n_fail} failed / {len(RESULTS)} total")
    if n_fail:
        print("FAILED CHECKS:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}: {detail}")
    print("=" * 70)
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
