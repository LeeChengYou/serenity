#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fund_pool.py — 模擬資金池引擎（paper trading，非投資建議）

功能：
  建池、下單（t1_open / latest_close）、撮合、NAV 記帳、AI 公司會診、記憶注入。

架構：
  - 池在 agents 表是 backend='human' 的特殊 agent，
    重用 agent_arena 的 connect / SLIPPAGE_BPS / build_briefing / _record_nav。
  - 不捏造數據；零 look-ahead（t1_open 嚴格遵守 decided_date < exec_date）。
  - 所有 LLM 呼叫透過 ConsultBackend 抽象；測試一律用 StubConsultBackend。

CLI：
  python scripts/fund_pool.py migrate
  python scripts/fund_pool.py daily
"""
from __future__ import annotations

import abc
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 重用 agent_arena 的工具函式與常數 ─────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import agent_arena as arena

connect = arena.connect
SLIPPAGE_BPS = arena.SLIPPAGE_BPS
build_briefing = arena.build_briefing

# ---------------------------------------------------------------------------
# 公開常數（介面契約）
# ---------------------------------------------------------------------------
DEFAULT_INITIAL_CASH: float = 3000.0
CONSULT_MAX_PARTICIPANTS: int = 5
CONSULT_MEMORY_K: int = 3
OUTCOME_TRADING_DAYS: int = 7


# ---------------------------------------------------------------------------
# migrate — 冪等建立 3 張新表 + agent_trades.fill_mode 欄
# ---------------------------------------------------------------------------

def migrate(con: sqlite3.Connection) -> None:
    """冪等。建立 pools / pool_consults / pool_consult_opinions；
    agent_trades 加 fill_mode 欄（已存在則略過）。"""
    con.executescript("""
        create table if not exists pools (
            agent_id     text primary key,
            display_name text not null,
            initial_cash real not null,
            status       text not null default 'active',
            created_at   text not null
        );

        create table if not exists pool_consults (
            id          integer primary key autoincrement,
            pool_id     text not null,
            trade_id    integer,
            symbol      text,
            as_of       text not null,
            question    text not null,
            summary     text,
            followed    integer,
            outcome_7d  real,
            created_at  text not null
        );

        create table if not exists pool_consult_opinions (
            id          integer primary key autoincrement,
            consult_id  integer not null,
            agent_id    text not null,
            stance      text not null,
            confidence  real,
            opinion     text not null
        );
    """)
    # agent_trades.fill_mode：若欄不存在則加入
    cols = {r[1] for r in con.execute("PRAGMA table_info(agent_trades)").fetchall()}
    if "fill_mode" not in cols:
        con.execute("ALTER TABLE agent_trades ADD COLUMN fill_mode text not null default 't1_open'")
    con.commit()


# ---------------------------------------------------------------------------
# create_pool
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """將名稱轉為 URL 安全 slug。"""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9一-鿿]+", "-", s)
    s = s.strip("-")
    return s or "pool"


def create_pool(
    con: sqlite3.Connection,
    name: str,
    initial_cash: float,
    created_at: str | None = None,
) -> str:
    """
    建立模擬資金池。
    回傳 pool_id='pool-<slug>'。
    同名池已存在 → raise ValueError。
    """
    slug = _slugify(name)
    pool_id = f"pool-{slug}"
    now_iso = created_at or datetime.utcnow().isoformat()

    # 同名重複檢查（display_name 唯一性）
    existing = con.execute(
        "SELECT agent_id FROM pools WHERE display_name=?", (name,)
    ).fetchone()
    if existing:
        raise ValueError(f"資金池名稱 '{name}' 已存在（id={existing['agent_id']}）")

    # --- agents 表 seed ---
    con.execute(
        """
        INSERT OR IGNORE INTO agents
            (id, domain, style_seed, backend, status, relaunches, hwm, created_at)
        VALUES (?, 'human', 'human', 'human', 'active', 0, ?, ?)
        """,
        (pool_id, initial_cash, now_iso),
    )

    # --- agent_state seed ---
    month_key = now_iso[:7]
    con.execute(
        """
        INSERT OR IGNORE INTO agent_state (agent_id, month, cash, nav, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (pool_id, month_key, initial_cash, initial_cash, now_iso),
    )

    # --- pools 表 seed ---
    con.execute(
        """
        INSERT OR IGNORE INTO pools
            (agent_id, display_name, initial_cash, status, created_at)
        VALUES (?, ?, ?, 'active', ?)
        """,
        (pool_id, name, initial_cash, now_iso),
    )
    con.commit()
    return pool_id


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------

def _get_current_cash(con: sqlite3.Connection, agent_id: str) -> float:
    row = con.execute(
        "SELECT cash FROM agent_state WHERE agent_id=? ORDER BY month DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return row["cash"] if row else 0.0


def _get_holding_qty(con: sqlite3.Connection, agent_id: str, symbol: str) -> float:
    row = con.execute(
        "SELECT qty FROM agent_positions WHERE agent_id=? AND symbol=?",
        (agent_id, symbol),
    ).fetchone()
    return row["qty"] if row else 0.0


def _apply_latest_close_fill(
    con: sqlite3.Connection,
    agent_id: str,
    side: str,
    symbol: str,
    usd: float | None,
    qty: float | None,
    fill_px: float,
    fill_date: str,
    as_of: str,
    reason: str,
) -> dict:
    """
    latest_close 模式：立即撮合並更新 positions / cash / agent_state。
    回傳 trade row dict（status='filled'）。
    """
    slip = SLIPPAGE_BPS / 10000.0
    cash = _get_current_cash(con, agent_id)

    if side == "BUY":
        actual_fill = fill_px * (1 + slip)
        actual_qty = (usd or 0.0) / actual_fill if actual_fill > 0 else 0.0
        cost = actual_qty * actual_fill
        new_cash = cash - cost

        # 更新持倉
        pos_row = con.execute(
            "SELECT qty, avg_cost FROM agent_positions WHERE agent_id=? AND symbol=?",
            (agent_id, symbol),
        ).fetchone()
        if pos_row:
            old_qty, old_cost = pos_row["qty"], pos_row["avg_cost"]
            new_qty = old_qty + actual_qty
            new_cost = (old_qty * old_cost + actual_qty * actual_fill) / new_qty if new_qty > 0 else actual_fill
            con.execute(
                "UPDATE agent_positions SET qty=?, avg_cost=?, updated_at=? WHERE agent_id=? AND symbol=?",
                (new_qty, new_cost, fill_date, agent_id, symbol),
            )
        else:
            con.execute(
                "INSERT INTO agent_positions (agent_id, symbol, qty, avg_cost, updated_at) VALUES (?,?,?,?,?)",
                (agent_id, symbol, actual_qty, actual_fill, fill_date),
            )
        fill_qty = actual_qty
        fill_price_out = actual_fill

    else:  # SELL
        actual_fill = fill_px * (1 - slip)
        actual_qty = qty or 0.0
        proceeds = actual_qty * actual_fill
        new_cash = cash + proceeds

        pos_row = con.execute(
            "SELECT qty FROM agent_positions WHERE agent_id=? AND symbol=?",
            (agent_id, symbol),
        ).fetchone()
        if pos_row:
            remaining = pos_row["qty"] - actual_qty
            if remaining <= 1e-8:
                con.execute(
                    "DELETE FROM agent_positions WHERE agent_id=? AND symbol=?",
                    (agent_id, symbol),
                )
            else:
                con.execute(
                    "UPDATE agent_positions SET qty=?, updated_at=? WHERE agent_id=? AND symbol=?",
                    (remaining, fill_date, agent_id, symbol),
                )
        fill_qty = actual_qty
        fill_price_out = actual_fill

    # 插入 agent_trades（filled）
    cur = con.execute(
        """
        INSERT INTO agent_trades
            (agent_id, decided_date, exec_date, symbol, side, qty, price,
             usd, cash_after, reason, status, fill_mode, briefing_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', 'latest_close', '')
        """,
        (
            agent_id, as_of, fill_date, symbol, side,
            fill_qty, fill_price_out,
            usd, new_cash,
            reason,
        ),
    )
    trade_id = cur.lastrowid  # 立即取 rowid，避免後續 INSERT 覆蓋 last_insert_rowid()

    # 更新 agent_state cash
    month_key = fill_date[:7]
    con.execute(
        "INSERT OR REPLACE INTO agent_state (agent_id, month, cash, nav, updated_at) VALUES (?,?,?,?,?)",
        (agent_id, month_key, new_cash, new_cash, fill_date),
    )
    con.commit()

    return {
        "status": "filled",
        "trade_id": trade_id,
        "rejected_reason": None,
        "fill_price": fill_price_out,
    }


def place_order(
    con: sqlite3.Connection,
    pool_id: str,
    side: str,
    symbol: str,
    *,
    usd: float | None = None,
    qty: float | None = None,
    reason: str,
    fill_mode: str = "t1_open",
    as_of: str,
) -> dict:
    """
    下單。
    回傳 {"status": "pending"|"filled"|"rejected", "trade_id": int|None,
          "rejected_reason": str|None, "fill_price": float|None}

    拒單順序：reason 空 → 池不存在/archived → symbol 不在 prices
              → BUY usd>現金 → SELL qty>持有
    """
    def reject(msg: str) -> dict:
        return {"status": "rejected", "trade_id": None, "rejected_reason": msg, "fill_price": None}

    # 1. reason 必填
    if not (reason or "").strip():
        return reject("reason 不能為空，每筆交易必須附理由")

    # 2. 池存在且非 archived
    pool_row = con.execute(
        "SELECT status FROM pools WHERE agent_id=?", (pool_id,)
    ).fetchone()
    if pool_row is None:
        return reject(f"資金池不存在：{pool_id}")
    if pool_row["status"] == "archived":
        return reject("資金池已封存，不接受新單")

    # 3. symbol 在 prices 表中有記錄
    sym_exists = con.execute(
        "SELECT 1 FROM prices WHERE symbol=? LIMIT 1", (symbol,)
    ).fetchone()
    if sym_exists is None:
        return reject(f"symbol {symbol!r} 不在 prices 表，無法下單")

    cash = _get_current_cash(con, pool_id)

    if side.upper() == "BUY":
        # 4. 現金不足
        if (usd or 0.0) > cash:
            return reject(f"現金不足：需 {usd:.2f}，現有 {cash:.2f}")

    elif side.upper() == "SELL":
        # 5. 持倉不足
        holding = _get_holding_qty(con, pool_id, symbol)
        if (qty or 0.0) > holding:
            return reject(f"持倉不足：要賣 {qty} 股，實際持有 {holding:.4f} 股")
    else:
        return reject(f"side 必須為 BUY 或 SELL，收到 {side!r}")

    # ── t1_open：插 pending，由 _fill_pending_orders 撮合 ──
    if fill_mode == "t1_open":
        if side.upper() == "BUY":
            cur = con.execute(
                """
                INSERT INTO agent_trades
                    (agent_id, decided_date, symbol, side, usd, reason, status, fill_mode, briefing_path)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 't1_open', '')
                """,
                (pool_id, as_of, symbol, side.upper(), usd, reason),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO agent_trades
                    (agent_id, decided_date, symbol, side, qty, reason, status, fill_mode, briefing_path)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 't1_open', '')
                """,
                (pool_id, as_of, symbol, side.upper(), qty, reason),
            )
        trade_id = cur.lastrowid
        con.commit()
        return {"status": "pending", "trade_id": trade_id, "rejected_reason": None, "fill_price": None}

    # ── latest_close：取最新 close <= as_of 立即成交 ──
    elif fill_mode == "latest_close":
        lc_row = con.execute(
            "SELECT date, close FROM prices WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT 1",
            (symbol, as_of),
        ).fetchone()
        if lc_row is None or lc_row["close"] is None:
            return reject(f"symbol {symbol!r} 在 {as_of} 前無可用收盤價")
        return _apply_latest_close_fill(
            con, pool_id, side.upper(), symbol,
            usd=usd, qty=qty,
            fill_px=lc_row["close"],
            fill_date=lc_row["date"],
            as_of=as_of,
            reason=reason,
        )
    else:
        return reject(f"不支援的 fill_mode：{fill_mode!r}")


# ---------------------------------------------------------------------------
# archive_pool
# ---------------------------------------------------------------------------

def archive_pool(con: sqlite3.Connection, pool_id: str) -> None:
    """封存資金池：pools.status='archived'，agents.status='retired'。"""
    con.execute("UPDATE pools SET status='archived' WHERE agent_id=?", (pool_id,))
    con.execute("UPDATE agents SET status='retired' WHERE id=?", (pool_id,))
    con.commit()


# ---------------------------------------------------------------------------
# list_pools
# ---------------------------------------------------------------------------

def list_pools(con: sqlite3.Connection) -> list[dict]:
    """列出所有資金池（含封存）。"""
    rows = con.execute(
        "SELECT agent_id, display_name, initial_cash, status, created_at FROM pools ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# ConsultBackend ABC
# ---------------------------------------------------------------------------

class ConsultBackend(abc.ABC):
    """AI 公司會診後端抽象。"""

    @abc.abstractmethod
    def opine(self, agent_id: str, prompt: str) -> dict:
        """
        回傳 {"stance": "support"|"oppose"|"neutral",
               "confidence": float, "opinion": str}
        失敗時 raise（由 run_consult 捕捉，標 absent）。
        """

    @abc.abstractmethod
    def synthesize(self, prompt: str) -> str:
        """回傳主席綜合報告文字。失敗時 raise。"""


class StubConsultBackend(ConsultBackend):
    """測試用 Stub。opine_map 值若為 callable 則呼叫它（可讓它 raise）。"""

    def __init__(self, opine_map: dict | None = None, synthesize_text: str = "stub summary"):
        self._opine_map = opine_map or {}
        self._synthesize_text = synthesize_text
        self.opine_prompts: list[tuple[str, str]] = []
        self.synthesize_prompts: list[str] = []

    def opine(self, agent_id: str, prompt: str) -> dict:
        self.opine_prompts.append((agent_id, prompt))
        val = self._opine_map.get(agent_id)
        if callable(val):
            val(agent_id, prompt)  # 可能 raise
        if isinstance(val, dict):
            return val
        return {"stance": "neutral", "confidence": 0.5, "opinion": f"stub opinion for {agent_id}"}

    def synthesize(self, prompt: str) -> str:
        self.synthesize_prompts.append(prompt)
        return self._synthesize_text


class GeminiConsultBackend(ConsultBackend):
    """正式 Gemini 後端。使用 serenity.gemini.call_gemini；要求 JSON 輸出。"""

    def opine(self, agent_id: str, prompt: str) -> dict:
        from serenity.gemini import call_gemini
        system = (
            "你是 AI 基金經理人，對使用者的投資議題表達立場。\n"
            "嚴格輸出 JSON（不含 markdown）：\n"
            '{"stance":"support|oppose|neutral","confidence":0.0~1.0,"opinion":"理由（≤150 字）"}'
        )
        res = call_gemini(
            model_name="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=system,
            temperature=0.3,
            response_mime_type="application/json",
            task_class="agent_arena",
        )
        text = res["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)  # 解析失敗 → raise（由 run_consult 捕捉標 absent），不靜默丟棄
        return parsed

    def synthesize(self, prompt: str) -> str:
        from serenity.gemini import call_gemini
        system = (
            "你是 AI 公司主席，彙整各位 AI 經理人的意見並撰寫綜合報告（繁體中文）。\n"
            "報告需包含：共識、分歧、多空論點、建議行動、主要風險。≤300 字純文字。"
        )
        res = call_gemini(
            model_name="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=system,
            temperature=0.3,
            task_class="agent_arena",
        )
        text = res["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()  # 失敗 → raise，不靜默丟棄


# ---------------------------------------------------------------------------
# run_consult
# ---------------------------------------------------------------------------

def run_consult(
    con: sqlite3.Connection,
    pool_id: str,
    question: str,
    symbol: str,
    participants: list[str],
    as_of: str,
    backend: ConsultBackend,
) -> int:
    """
    AI 公司會診。
    回傳 consult_id（int）。

    流程：
      1. 意見輪：每位 participant 呼叫 backend.opine()；失敗 → stance='absent'。
      2. 綜合輪：≥1 份非 absent 意見 → backend.synthesize()；全 absent → summary=NULL。
      3. 落庫 pool_consults + pool_consult_opinions。
    """
    now_iso = datetime.utcnow().isoformat()

    # 取得本池同 symbol 最近 CONSULT_MEMORY_K 次會診（記憶注入）
    mem_rows = con.execute(
        """
        SELECT summary, outcome_7d, followed, as_of
        FROM pool_consults
        WHERE pool_id=? AND symbol=? AND summary IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (pool_id, symbol, CONSULT_MEMORY_K),
    ).fetchall()
    memory_fragment = ""
    if mem_rows:
        parts = []
        for m in reversed(mem_rows):
            o7d_str = f"{m['outcome_7d']:.2%}" if m["outcome_7d"] is not None else "未知"
            followed_str = "照做" if m["followed"] == 1 else ("未照做" if m["followed"] == 0 else "未記錄")
            parts.append(
                f"[{m['as_of']}] 摘要：{m['summary'] or '—'}  "
                f"事後7日報酬：{o7d_str}  是否照做：{followed_str}"
            )
        memory_fragment = "\n## 歷史會診記憶（同 symbol，最近 K 次）\n" + "\n".join(parts) + "\n"

    # 意見輪
    opinions: list[dict] = []
    for agent_id in participants:
        # 取得 agent briefing（零 look-ahead：as_of 日 briefing）
        agent_row = con.execute(
            "SELECT domain FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        agent_domain = agent_row["domain"] if agent_row else "semis"
        try:
            briefing = build_briefing(con, agent_domain, as_of, agent_id)
        except Exception:
            briefing = f"（{agent_id} 當日簡報載入失敗）"

        # 取得 agent 的個人記憶
        mem_personal = con.execute(
            "SELECT content FROM agent_memory WHERE agent_id=? ORDER BY id DESC LIMIT 3",
            (agent_id,),
        ).fetchall()
        personal_fragment = ""
        if mem_personal:
            personal_fragment = "\n## 你的個人備忘\n" + "\n".join(r["content"] for r in mem_personal) + "\n"

        prompt = (
            f"## 議題\n{question}\n\n"
            f"## 標的\n{symbol}\n\n"
            f"## 市場簡報（{as_of}）\n{briefing}\n"
            + personal_fragment
            + memory_fragment
        )

        try:
            result = backend.opine(agent_id, prompt)
            stance = result.get("stance", "neutral")
            if stance not in ("support", "oppose", "neutral"):
                stance = "neutral"
            confidence = float(result.get("confidence") or 0.5)
            opinion_text = str(result.get("opinion") or "")
        except Exception:
            stance = "absent"
            confidence = None
            opinion_text = "（呼叫失敗或 429）"

        opinions.append({
            "agent_id": agent_id,
            "stance": stance,
            "confidence": confidence,
            "opinion": opinion_text,
        })

    # 綜合輪
    non_absent = [o for o in opinions if o["stance"] != "absent"]
    summary: str | None = None
    if non_absent:
        opinions_text = "\n".join(
            f"[{o['agent_id']}] 立場={o['stance']} 信心={o['confidence']:.2f}\n理由：{o['opinion']}"
            for o in non_absent
        )
        synth_prompt = (
            f"## 議題\n{question}\n\n## 標的\n{symbol}\n\n"
            f"## 各位 AI 經理人意見\n{opinions_text}\n"
            + memory_fragment
        )
        try:
            summary = backend.synthesize(synth_prompt)
        except Exception:
            summary = None

    # 落庫 pool_consults
    con.execute(
        """
        INSERT INTO pool_consults (pool_id, symbol, as_of, question, summary, followed, outcome_7d, created_at)
        VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (pool_id, symbol, as_of, question, summary, now_iso),
    )
    consult_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

    # 落庫 pool_consult_opinions
    for o in opinions:
        con.execute(
            """
            INSERT INTO pool_consult_opinions (consult_id, agent_id, stance, confidence, opinion)
            VALUES (?, ?, ?, ?, ?)
            """,
            (consult_id, o["agent_id"], o["stance"], o["confidence"], o["opinion"]),
        )
    con.commit()
    return consult_id


# ---------------------------------------------------------------------------
# backfill_outcomes — 冪等
# ---------------------------------------------------------------------------

def backfill_outcomes(con: sqlite3.Connection, as_of: str) -> None:
    """
    冪等。對 outcome_7d IS NULL 且 symbol 非 NULL 的會診：
    若 prices 中該 symbol 在會診日之後已有 ≥ OUTCOME_TRADING_DAYS 個交易日，
    outcome_7d = 第 7 交易日 close / 會診日最近 close - 1。
    缺價維持 NULL（零捏造）。
    """
    pending = con.execute(
        "SELECT id, pool_id, symbol, as_of FROM pool_consults "
        "WHERE outcome_7d IS NULL AND symbol IS NOT NULL",
    ).fetchall()

    for row in pending:
        consult_as_of = row["as_of"]
        symbol = row["symbol"]

        # 只處理截止 as_of 以前的會診（未到期的不強算）
        if consult_as_of > as_of:
            continue

        # 取得會診日最近 close
        base_row = con.execute(
            "SELECT close FROM prices WHERE symbol=? AND date <= ? ORDER BY date DESC LIMIT 1",
            (symbol, consult_as_of),
        ).fetchone()
        if base_row is None or base_row["close"] is None:
            continue

        # 取得會診日之後的交易日序列
        future_rows = con.execute(
            "SELECT date, close FROM prices WHERE symbol=? AND date > ? ORDER BY date",
            (symbol, consult_as_of),
        ).fetchall()

        if len(future_rows) < OUTCOME_TRADING_DAYS:
            continue  # 不足 7 日，維持 NULL

        day7_close = future_rows[OUTCOME_TRADING_DAYS - 1]["close"]
        if day7_close is None:
            continue

        outcome = day7_close / base_row["close"] - 1.0
        con.execute(
            "UPDATE pool_consults SET outcome_7d=? WHERE id=?",
            (outcome, row["id"]),
        )

    con.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_migrate(args):
    db_path = args.db or (ROOT / "data" / "serenity.sqlite")
    con = connect(db_path)
    arena.migrate(con)
    migrate(con)
    con.close()
    print("fund_pool migrate 完成（冪等）。")


def cmd_daily(args):
    db_path = args.db or (ROOT / "data" / "serenity.sqlite")
    con = connect(db_path)
    arena.migrate(con)
    migrate(con)
    # 取最新交易日
    row = con.execute("SELECT MAX(date) FROM prices").fetchone()
    as_of = row[0] if row and row[0] else datetime.utcnow().strftime("%Y-%m-%d")
    backfill_outcomes(con, as_of)
    con.close()
    print(f"fund_pool daily {as_of} 完成（backfill_outcomes）。")


def main():
    parser = argparse.ArgumentParser(description="模擬資金池引擎（fund_pool）")
    parser.add_argument("--db", metavar="PATH", help="覆寫資料庫路徑")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="冪等建立資料表")
    sub.add_parser("daily", help="執行每日 backfill_outcomes")
    args = parser.parse_args()
    if args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "daily":
        cmd_daily(args)


if __name__ == "__main__":
    main()
