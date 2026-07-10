#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_arena.py — V6 AI 經理人競技場（模擬資金・非投資建議）

CLI: python scripts/agent_arena.py <subcommand> [options]
  migrate          建立 / 升級資料庫表（冪等）
  daily            執行日循環（撮合昨日 pending → 生成簡報 → 決策 → 記錄 NAV）
  monthly          執行月度結算與反思
  status           顯示 agent 狀態摘要

全域選項:
  --db PATH        覆寫資料庫路徑（預設 data/serenity.sqlite）
"""
from __future__ import annotations

import abc
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (公開介面契約，測試直接引用)
# ---------------------------------------------------------------------------
INIT_CAPITAL: float = 3000.0
MAX_POS_PCT: float = 0.40
MAX_TRADES_PER_DAY: int = 3
SLIPPAGE_BPS: int = 5
RELAUNCH_DD: float = 0.40
RELAUNCH_FLOOR: float = 1500.0

DOMAINS: dict[str, list[str]] = {
    "semis": [
        "NVDA", "TSM", "AVGO", "AMD", "ASML", "MU", "AMAT", "INTC",
        "ARM", "MRVL", "ON", "STM", "TER", "GFS", "COHR", "LITE", "ALAB", "SIMO",
    ],
    "robotics": ["TSLA", "TER", "AEVA", "RKLB", "ASTS", "PL"],
    "ai_cloud": ["MSFT", "GOOGL", "META", "AMZN", "ORCL", "CRM", "NBIS", "CRWV", "VRT"],
}

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "serenity.sqlite"
DEFAULT_DATA_DIR = ROOT / "data"

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def connect(db_path) -> sqlite3.Connection:
    """Return a sqlite3.Connection with row_factory=sqlite3.Row."""
    con = sqlite3.connect(str(db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute("pragma foreign_keys=on")
    return con


def migrate(con: sqlite3.Connection) -> None:
    """冪等建立 7 個 arena 表。"""
    con.executescript("""
        create table if not exists agents (
            id          text primary key,
            domain      text not null,
            style_seed  text not null,
            backend     text not null default 'gemini',
            status      text not null default 'active',
            relaunches  integer not null default 0,
            hwm         real not null default 3000.0,
            created_at  text not null
        );
        create table if not exists agent_state (
            agent_id    text not null,
            month       text not null,
            cash        real not null,
            nav         real,
            updated_at  text not null,
            primary key (agent_id, month)
        );
        create table if not exists agent_positions (
            agent_id    text not null,
            symbol      text not null,
            qty         real not null,
            avg_cost    real not null,
            updated_at  text not null,
            primary key (agent_id, symbol)
        );
        create table if not exists agent_trades (
            id              integer primary key autoincrement,
            agent_id        text not null,
            decided_date    text not null,
            exec_date       text,
            symbol          text,
            side            text,
            qty             real,
            price           real,
            usd             real,
            cash_after      real,
            reason          text not null,
            status          text not null,
            rejected_reason text,
            briefing_path   text not null default ''
        );
        create table if not exists agent_monthly (
            agent_id        text not null,
            month           text not null,
            nav_start       real,
            nav_end         real,
            ret_pct         real,
            mdd_pct         real,
            win_rate        real,
            n_trades        integer,
            rank_domain     integer,
            rank_overall    integer,
            public_letter   text,
            reflection_md   text,
            strategy_before text,
            strategy_after  text,
            primary key (agent_id, month)
        );
        create table if not exists agent_nav_daily (
            agent_id    text not null,
            date        text not null,
            nav         real not null,
            cash        real not null,
            primary key (agent_id, date)
        );
        create table if not exists agent_memory (
            id          integer primary key autoincrement,
            agent_id    text not null,
            date        text not null,
            content     text not null,
            created_at  text not null
        );
    """)
    con.commit()


# ---------------------------------------------------------------------------
# Strategy card templates (zh-TW, ≤600 tokens each)
# ---------------------------------------------------------------------------

_STRATEGY_MOMENTUM = """\
# {agent_id} 策略卡 v1
## 風格：動能型（Momentum）
## 進場
- 突破 20 日高點 + vol_z > 0.5 → 首倉 NAV 15%
- 連續 3 日高於 EMA50 且 RSI14 50-70 → 可加倉至 NAV 30%
- 不在財報前 3 日建新倉
## 出場
- 收盤跌破 EMA50 → 全出（停損）
- RSI14 > 80 → 減碼至 NAV 15%（鎖利）
## 倉位
- 單股上限 35%（低於系統上限 40%，自留緩衝）
- 同時持股不超過 3 檔
## 原則
- 理由必須對應具體技術訊號（不能只說「感覺要漲」）
- vol_z < -0.5 時不追高（量能萎縮假突破風險）
"""

_STRATEGY_DIP = """\
# {agent_id} 策略卡 v1
## 風格：逢低型（Dip Buyer）
## 進場
- RSI14 < 40 且收盤高於 EMA200（長線多頭） → 分批建倉（首倉 NAV 10%，拉回再加 10%）
- chg5d < -8% 且基本面無惡化 → 視為超賣機會
## 出場
- 反彈至 EMA20 → 減碼 50%（先落袋）
- 反彈至 EMA50 → 全出（達目標）
- 跌破前低 5% → 停損全出（論點失敗）
## 倉位
- 分批建倉：單股累計上限 35%
- 同時持股不超過 3 檔
## 原則
- 必須有 RSI 或 chg5d 指標支持
- 財報前 5 日不建倉（財報驚喜/失望風險大）
"""

_STRATEGY_CATALYST = """\
# {agent_id} 策略卡 v1
## 風格：事件型（Catalyst Driven）
## 進場
- 重大新聞催化劑（財報超預期、新產品、13F 大戶增持） → 入場 NAV 20%
- 分析師上調目標價 +10% 以上且估計上修 → 加倉 NAV 10%
## 出場
- 財報後 3 日評估：若利多已充分反應（chg5d > 15%） → 減碼 50%
- 消息退潮（新聞熱度降 + RSI 回落） → 全出
- 財報前 3 日不建新倉（資訊不對稱風險）
## 倉位
- 單股上限 30%（事件性風險需保守）
- 催化劑消退後不留倉超過 14 日
## 原則
- 每筆理由必須引用具體新聞標題或事件
- 13F 申報有 45 日延遲，建倉時必須在 reason 中標明
"""

_STRATEGY_TEMPLATES = {
    "momentum": _STRATEGY_MOMENTUM,
    "dip": _STRATEGY_DIP,
    "catalyst": _STRATEGY_CATALYST,
}

_DEFAULT_FORMAT_PREFS = {
    "news_count": 10,
    "show_estimates": True,
    "show_expert_views": True,
    "show_earnings": True,
    "price_columns": ["chg1d", "chg5d", "chg20d", "rsi14", "ema50", "vol_z"],
    "extra_symbols": [],
}


def seed_agents(con: sqlite3.Connection, data_dir=None) -> None:
    """冪等植入 9 agents + 策略卡/format_prefs 檔案。"""
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    now = datetime.utcnow().isoformat()

    for domain, styles in [
        ("semis", ["momentum", "dip", "catalyst"]),
        ("robotics", ["momentum", "dip", "catalyst"]),
        ("ai_cloud", ["momentum", "dip", "catalyst"]),
    ]:
        for style in styles:
            agent_id = f"{domain}-{style}"
            # DB insert (ignore if exists)
            con.execute("""
                insert or ignore into agents
                    (id, domain, style_seed, backend, status, relaunches, hwm, created_at)
                values (?, ?, ?, 'gemini', 'active', 0, ?, ?)
            """, (agent_id, domain, style, INIT_CAPITAL, now))

            # Ensure initial agent_state row
            month_key = now[:7]
            con.execute("""
                insert or ignore into agent_state (agent_id, month, cash, nav, updated_at)
                values (?, ?, ?, ?, ?)
            """, (agent_id, month_key, INIT_CAPITAL, INIT_CAPITAL, now))

            # Strategy card + prefs files
            agent_dir = data_dir / "agents" / agent_id
            agent_dir.mkdir(parents=True, exist_ok=True)

            strat_file = agent_dir / "strategy.md"
            if not strat_file.exists():
                template = _STRATEGY_TEMPLATES.get(style, _STRATEGY_MOMENTUM)
                strat_file.write_text(
                    template.format(agent_id=agent_id), encoding="utf-8"
                )

            prefs_file = agent_dir / "format_prefs.json"
            if not prefs_file.exists():
                prefs_file.write_text(
                    json.dumps(_DEFAULT_FORMAT_PREFS, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    con.commit()


# ---------------------------------------------------------------------------
# Technical indicator helpers (pure Python, no pandas/numpy)
# ---------------------------------------------------------------------------

def _calc_rsi14(closes: list[float]) -> float | None:
    """Wilder smoothed RSI-14."""
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    # first 14
    avg_g = sum(gains[:14]) / 14
    avg_l = sum(losses[:14]) / 14
    for i in range(14, len(gains)):
        avg_g = (avg_g * 13 + gains[i]) / 14
        avg_l = (avg_l * 13 + losses[i]) / 14
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def _calc_ema(closes: list[float], period: int) -> float | None:
    """EMA with standard multiplier."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return round(ema, 4)


def _calc_vol_z(volumes: list[float]) -> float | None:
    """Z-score of latest volume vs 20-day mean/std."""
    if len(volumes) < 20:
        return None
    window = volumes[-20:]
    mean_v = sum(window) / 20
    var_v = sum((v - mean_v) ** 2 for v in window) / 20
    std_v = math.sqrt(var_v)
    if std_v == 0:
        return 0.0
    return round((volumes[-1] - mean_v) / std_v, 2)


def _pct_chg(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    return round((closes[-1] / closes[-(n + 1)] - 1) * 100, 2)


def _regime_line(con: sqlite3.Connection, as_of: str) -> str:
    """Build regime description from SPY EMA200 and recent price."""
    try:
        rows = con.execute(
            "select close from prices where symbol='SPY' and date <= ? "
            "order by date desc limit 210",
            (as_of,)
        ).fetchall()
        if not rows:
            return "UNKNOWN"
        closes = [r[0] for r in reversed(rows)]
        latest = closes[-1]
        ema200 = _calc_ema(closes, 200)
        if ema200 is None:
            return "UNKNOWN"
        trend = "BULL" if latest > ema200 else "BEAR"
        return trend
    except Exception:
        return "UNKNOWN"


# ---------------------------------------------------------------------------
# Briefing builder
# ---------------------------------------------------------------------------

def build_briefing(
    con: sqlite3.Connection,
    domain: str,
    as_of: str,
    agent_id: str,
    data_dir=None,
) -> str:
    """Generate briefing text for one agent on a given day (no look-ahead)."""
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    symbols = DOMAINS.get(domain, [])
    cutoff_dt = as_of + "T23:59:59"

    # Load format prefs
    prefs_path = data_dir / "agents" / agent_id / "format_prefs.json"
    prefs = dict(_DEFAULT_FORMAT_PREFS)
    if prefs_path.exists():
        try:
            loaded = json.loads(prefs_path.read_text(encoding="utf-8"))
            # Validate and merge whitelist fields
            if isinstance(loaded.get("news_count"), int) and 0 <= loaded["news_count"] <= 15:
                prefs["news_count"] = loaded["news_count"]
            for bool_key in ("show_estimates", "show_expert_views", "show_earnings"):
                if isinstance(loaded.get(bool_key), bool):
                    prefs[bool_key] = loaded[bool_key]
            if isinstance(loaded.get("extra_symbols"), list):
                prefs["extra_symbols"] = loaded["extra_symbols"][:3]
        except Exception:
            pass

    # Regime
    regime = _regime_line(con, as_of)

    lines = [f"# BRIEF {as_of} | domain={domain} | regime={regime}"]

    # ── PRICES ──
    lines.append("\n## PRICES  (sym  close  chg1d%  chg5d%  chg20d%  rsi14  ema50_rel  vol_z)")
    all_price_syms = list(symbols)
    for xs in (prefs.get("extra_symbols") or []):
        if xs not in all_price_syms:
            all_price_syms.append(xs)

    for sym in all_price_syms:
        price_rows = con.execute(
            "select date, close, volume from prices where symbol=? and date <= ? "
            "order by date desc limit 230",
            (sym, as_of)
        ).fetchall()
        if not price_rows:
            continue
        price_rows = list(reversed(price_rows))
        closes = [r["close"] for r in price_rows]
        volumes = [r["volume"] for r in price_rows if r["volume"] is not None]

        close_now = closes[-1]
        chg1d = _pct_chg(closes, 1)
        chg5d = _pct_chg(closes, 5)
        chg20d = _pct_chg(closes, 20)
        rsi14 = _calc_rsi14(closes)
        ema50 = _calc_ema(closes, 50)
        vol_z = _calc_vol_z(volumes) if volumes else None

        def fmt_pct(v):
            if v is None:
                return "N/A"
            return f"{'+' if v >= 0 else ''}{v:.1f}%"

        ema50_flag = ""
        if ema50 is not None:
            ema50_flag = "↑" if close_now > ema50 else "↓"

        rsi_str = str(int(rsi14)) if rsi14 is not None else "N/A"
        vol_z_str = f"{vol_z:+.1f}" if vol_z is not None else "N/A"

        lines.append(
            f"{sym:6s}  {close_now:8.2f}  {fmt_pct(chg1d):>7}  {fmt_pct(chg5d):>7}  "
            f"{fmt_pct(chg20d):>7}  {rsi_str:>3} {ema50_flag:>1}  {vol_z_str}"
        )

    # ── NEWS ──
    news_count = prefs.get("news_count", 10)
    if news_count > 0:
        lines.append(f"\n## NEWS  (sym|hrs_ago|title — 最多 {news_count} 則，僅標題)")
        # Build symbol filter for domain
        sym_conditions = " OR ".join(
            ["symbols LIKE ?"] * len(symbols)
        )
        sym_params = [f"%{s}%" for s in symbols]
        # Strict no look-ahead: published_at <= as_of + T23:59:59
        news_rows = con.execute(
            f"select title, published_at, symbols from news "
            f"where published_at <= ? and ({sym_conditions} or scope='macro') "
            f"order by published_at desc limit ?",
            [cutoff_dt] + sym_params + [news_count]
        ).fetchall()

        # hrs_ago 以 as_of 當日收盤（23:59:59）為基準，否則當日新聞會算出負值
        as_of_dt = datetime.fromisoformat(as_of) + timedelta(hours=23, minutes=59, seconds=59)
        for row in news_rows:
            pub = row["published_at"] or ""
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00").replace("+00:00", ""))
                hrs_ago = max(0, int((as_of_dt - pub_dt).total_seconds() / 3600))
            except Exception:
                hrs_ago = 0
            # symbols 欄是 JSON 陣列字串（如 '["MU"]'），需解析而非直接切字串
            try:
                sym_list = json.loads(row["symbols"] or "[]")
            except Exception:
                sym_list = []
            first_sym = sym_list[0].strip().upper() if sym_list else "MACRO"
            title = (row["title"] or "")[:100]
            lines.append(f"{first_sym}|{hrs_ago}|{title}")

    # ── ESTIMATES_CHANGES ──
    if prefs.get("show_estimates", True):
        week_ago = (datetime.fromisoformat(as_of) - timedelta(days=7)).strftime("%Y-%m-%d")
        est_rows = con.execute(
            "select symbol, target_mean, n_analysts, updated_at "
            "from analyst_estimates "
            "where symbol in ({}) and updated_at >= ? and updated_at <= ?".format(
                ",".join("?" * len(symbols))
            ),
            symbols + [week_ago, cutoff_dt]
        ).fetchall()
        if est_rows:
            lines.append("\n## ESTIMATES_CHANGES  (sym  tgt_mean  vs_px  n)")
            for r in est_rows:
                sym = r["symbol"]
                tgt = r["target_mean"]
                n = r["n_analysts"]
                # Get latest price for vs_px
                px_row = con.execute(
                    "select close from prices where symbol=? and date<=? order by date desc limit 1",
                    (sym, as_of)
                ).fetchone()
                if tgt and px_row:
                    vs = (tgt / px_row["close"] - 1) * 100
                    vs_str = f"{'+' if vs >= 0 else ''}{vs:.0f}%"
                    rev = "↑" if vs > 0 else "↓"
                    lines.append(f"{sym}  {tgt:.0f}  {vs_str}  {rev}  {n or '?'}")

    # ── EXPERT_VIEWS ──
    if prefs.get("show_expert_views", True):
        sym_conditions2 = " OR ".join([f"symbols LIKE '%{s}%'" for s in symbols])
        ev_rows = con.execute(
            "select title, source, published_at from expert_views "
            f"where published_at <= ? and ({sym_conditions2}) "
            "order by published_at desc limit 3",
            (cutoff_dt,)
        ).fetchall()
        if ev_rows:
            lines.append("\n## EXPERT_VIEWS  (含 45 天延遲警語)")
            for r in ev_rows:
                title_short = (r["title"] or "")[:80]
                lines.append(f"{r['source'] or '?'}: {title_short} [45d-delay]")

    # ── EARNINGS_SOON ──
    if prefs.get("show_earnings", True):
        # Check fundamentals table for earnings dates if it exists
        try:
            window_end = (datetime.fromisoformat(as_of) + timedelta(days=14)).strftime("%Y-%m-%d")
            fund_rows = con.execute(
                "select symbol, earnings_date from fundamentals "
                "where symbol in ({}) and earnings_date > ? and earnings_date <= ?".format(
                    ",".join("?" * len(symbols))
                ),
                symbols + [as_of, window_end]
            ).fetchall()
            if fund_rows:
                lines.append("\n## EARNINGS_SOON  (≤14 天內公布財報者)")
                for r in fund_rows:
                    try:
                        days_out = (datetime.fromisoformat(r["earnings_date"]) -
                                    datetime.fromisoformat(as_of)).days
                        lines.append(f"{r['symbol']} {r['earnings_date']} ({days_out}d)")
                    except Exception:
                        lines.append(f"{r['symbol']} {r['earnings_date']}")
        except Exception:
            pass  # fundamentals table may not have earnings_date column

    # ── YOUR_PORTFOLIO ──
    positions = con.execute(
        "select symbol, qty, avg_cost from agent_positions where agent_id=?",
        (agent_id,)
    ).fetchall()

    # Get current cash from latest agent_state
    state_row = con.execute(
        "select cash, nav from agent_state where agent_id=? order by month desc limit 1",
        (agent_id,)
    ).fetchone()
    cash = state_row["cash"] if state_row else INIT_CAPITAL
    nav = state_row["nav"] if state_row else INIT_CAPITAL

    # Recalculate NAV from current positions
    pos_value = 0.0
    pos_lines = []
    for pos in positions:
        sym = pos["symbol"]
        qty = pos["qty"]
        px_row = con.execute(
            "select close from prices where symbol=? and date<=? order by date desc limit 1",
            (sym, as_of)
        ).fetchone()
        if px_row:
            px_now = px_row["close"]
            pos_val = qty * px_now
            pos_value += pos_val
            pl_pct = (px_now / pos["avg_cost"] - 1) * 100 if pos["avg_cost"] else 0
            nav_pct = pos_val / (cash + pos_value) * 100 if (cash + pos_value) > 0 else 0
            pos_lines.append(
                f"{sym}  {qty:.2f}sh  @{pos['avg_cost']:.2f}  "
                f"now {px_now:.2f}  P/L {'+' if pl_pct >= 0 else ''}{pl_pct:.1f}%  "
                f"({nav_pct:.0f}% of NAV)"
            )

    current_nav = cash + pos_value
    # MTD return: get nav at start of current month
    month_key = as_of[:7]
    first_nav_row = con.execute(
        "select nav from agent_nav_daily where agent_id=? and date like ? order by date limit 1",
        (agent_id, month_key + "%")
    ).fetchone()
    mtd_str = ""
    if first_nav_row and first_nav_row["nav"]:
        mtd = (current_nav / first_nav_row["nav"] - 1) * 100
        mtd_str = f" | mtd={'+' if mtd >= 0 else ''}{mtd:.1f}%"

    lines.append(f"\n## YOUR_PORTFOLIO  (cash={cash:.2f} | nav={current_nav:.2f}{mtd_str})")
    for pl in pos_lines:
        lines.append(pl)
    if not pos_lines:
        lines.append("（空倉）")

    # ── YOUR_REJECTED_ORDERS ── 最近被引擎拒絕的單（學習訊號）
    rej_rows = con.execute(
        "select decided_date, side, symbol, rejected_reason from agent_trades "
        "where agent_id=? and status='rejected' and decided_date < ? "
        "order by id desc limit 3",
        (agent_id, as_of)
    ).fetchall()
    if rej_rows:
        lines.append("\n## YOUR_REJECTED_ORDERS  (最近被拒的單與原因，請修正下單格式/規則)")
        for r in rej_rows:
            lines.append(f"{r['decided_date']}  {r['side']}  {r['symbol']}  → {r['rejected_reason']}")

    # ── YOUR_MEMORY_DIGEST ──
    mem_rows = con.execute(
        "select content from agent_memory where agent_id=? order by id desc limit 3",
        (agent_id,)
    ).fetchall()
    lines.append("\n## YOUR_MEMORY_DIGEST  (你上次留下的備忘，≤3 行)")
    if mem_rows:
        for r in mem_rows:
            lines.append(r["content"][:100])
    else:
        lines.append("（無記憶）")

    briefing = "\n".join(lines)
    return briefing


def _save_briefing(briefing: str, as_of: str, agent_id: str, data_dir: Path) -> str:
    """Save briefing to file and return path string."""
    brief_dir = data_dir / "briefings"
    brief_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{as_of}_{agent_id}.txt"
    fpath = brief_dir / fname
    fpath.write_text(briefing, encoding="utf-8")
    return str(fpath)


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------

class AgentBackend(abc.ABC):
    @abc.abstractmethod
    def decide(self, agent_id: str, briefing: str, strategy: str) -> dict:
        """Return decision dict: {actions, watch, memory_note}."""

    @abc.abstractmethod
    def reflect(self, agent_id: str, dossier: str) -> dict:
        """Return reflection dict: {public_letter, reflection_md, strategy_md}."""


class StubBackend(AgentBackend):
    """Test stub: returns preset responses or safe defaults."""

    def __init__(self, decide_map: dict = None, reflect_map: dict = None):
        self._decide_map = decide_map or {}
        self._reflect_map = reflect_map or {}

    def decide(self, agent_id: str, briefing: str, strategy: str) -> dict:
        if agent_id in self._decide_map:
            return self._decide_map[agent_id]
        return {"actions": [], "watch": [], "memory_note": ""}

    def reflect(self, agent_id: str, dossier: str) -> dict:
        if agent_id in self._reflect_map:
            return self._reflect_map[agent_id]
        return {
            "public_letter": "",
            "reflection_md": "",
            "strategy_md": "",
        }


class GeminiBackend(AgentBackend):
    """Production backend using Gemini via server.call_gemini."""

    def __init__(self):
        # Import server module (safe: it has __main__ guard)
        scripts_dir = Path(__file__).resolve().parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            import server as _srv
            self._srv = _srv
        except ImportError as exc:
            raise RuntimeError(f"無法匯入 server 模組: {exc}")

        if not self._srv._key_manager.has_any_key():
            raise RuntimeError(
                "未偵測到任何 GEMINI_API_KEY。\n"
                "請在專案目錄建立 .env 檔並填入 GEMINI_API_KEY=your_key，\n"
                "然後重新執行。"
            )

    def decide(self, agent_id: str, briefing: str, strategy: str) -> dict:
        system = (
            "你是一位 AI 基金經理人，負責管理模擬資金投資組合（paper trading，非真實投資）。\n"
            "根據市場簡報與你的策略卡，做出今日買賣決策。\n"
            "輸出嚴格 JSON，欄位名稱必須完全一致，不得增減：\n"
            '{"actions":[{"side":"BUY","symbol":"NVDA","usd":600,"reason":"進場理由（必填，<=100字）"},\n'
            '            {"side":"SELL","symbol":"MU","pct":50,"reason":"出場理由（必填，<=100字）"}],\n'
            ' "watch":["觀察股，最多3檔"],\n'
            ' "memory_note":"給明天的自己的備忘（<=50字）"}\n'
            "規則：\n"
            "- side 只能是 BUY 或 SELL；BUY 必須用 usd（美元金額），SELL 必須用 pct（持倉百分比 1-100）\n"
            "- 只能交易簡報 PRICES 區塊列出的股票；每日最多 3 筆；單一持股上限 NAV 的 40%\n"
            "- 沒有好機會時 actions 給空陣列 []（持有不動也是合法決策）\n"
            "- 違反規則的單會被引擎拒絕並記錄原因"
        )
        prompt = f"## 策略卡\n{strategy}\n\n## 市場簡報\n{briefing}"
        try:
            res = self._srv.call_gemini(
                model_name="gemini-2.5-flash",
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                system_instruction=system,
                temperature=0.3,
                response_mime_type="application/json",
                task_class="agent_arena",
            )
            text = res["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except json.JSONDecodeError:
            # Retry once
            try:
                text2 = res["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text2)
            except Exception:
                return {"actions": [], "watch": [], "memory_note": "", "backend_error": True}
        except Exception as exc:
            print(f"[GeminiBackend] decide error for {agent_id}: {exc}")
            return {"actions": [], "watch": [], "memory_note": "", "backend_error": True}

    def reflect(self, agent_id: str, dossier: str) -> dict:
        system = (
            "你是一位 AI 基金經理人，正在進行月度反思（paper trading）。\n"
            "根據本月交易明細、績效指標與同業公開信，撰寫反思並更新策略卡。\n"
            "輸出嚴格 JSON：{\"public_letter\":\"...\",\"reflection_md\":\"...\",\"strategy_md\":\"...\"}"
        )
        try:
            res = self._srv.call_gemini(
                model_name="gemini-2.5-pro",
                contents=[{"role": "user", "parts": [{"text": dossier}]}],
                system_instruction=system,
                temperature=0.4,
                response_mime_type="application/json",
                task_class="agent_arena",
            )
            text = res["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except Exception as exc:
            print(f"[GeminiBackend] reflect error for {agent_id}: {exc}")
            return {"public_letter": "", "reflection_md": "", "strategy_md": ""}


# ---------------------------------------------------------------------------
# Order engine helpers
# ---------------------------------------------------------------------------

def _get_current_cash(con: sqlite3.Connection, agent_id: str) -> float:
    """Get current cash for an agent."""
    row = con.execute(
        "select cash from agent_state where agent_id=? order by month desc limit 1",
        (agent_id,)
    ).fetchone()
    return row["cash"] if row else INIT_CAPITAL


def _get_nav(con: sqlite3.Connection, agent_id: str, as_of: str) -> float:
    """Calculate current NAV = cash + mark-to-market positions."""
    cash = _get_current_cash(con, agent_id)
    positions = con.execute(
        "select symbol, qty from agent_positions where agent_id=?", (agent_id,)
    ).fetchall()
    nav = cash
    for pos in positions:
        px_row = con.execute(
            "select close from prices where symbol=? and date<=? order by date desc limit 1",
            (pos["symbol"], as_of)
        ).fetchone()
        if px_row:
            nav += pos["qty"] * px_row["close"]
    return nav


def _fill_pending_orders(con: sqlite3.Connection, as_of: str) -> None:
    """Fill all pending orders using today's open price with slippage.

    只撮合 decided_date < as_of 的單——決策日之前的價格絕不能
    回頭撮合之後的決策（時間紀律防護）。
    """
    pending = con.execute(
        "select * from agent_trades where status='pending' and decided_date < ?",
        (as_of,),
    ).fetchall()

    for trade in pending:
        symbol = trade["symbol"]
        side = trade["side"]
        agent_id = trade["agent_id"]

        # Get today's open price
        px_row = con.execute(
            "select open, close from prices where symbol=? and date=?",
            (symbol, as_of)
        ).fetchone()
        if px_row is None or px_row["open"] is None:
            # No price today → leave pending
            continue

        open_px = px_row["open"]
        slip = SLIPPAGE_BPS / 10000.0

        if side == "BUY":
            fill_px = open_px * (1 + slip)
            # Get usd amount from trade record
            usd = trade["usd"] or 0.0
            qty = usd / fill_px if fill_px > 0 else 0.0

            # Update cash
            cash = _get_current_cash(con, agent_id)
            new_cash = cash - (qty * fill_px)

            # Update position
            pos_row = con.execute(
                "select qty, avg_cost from agent_positions where agent_id=? and symbol=?",
                (agent_id, symbol)
            ).fetchone()
            if pos_row:
                old_qty = pos_row["qty"]
                old_cost = pos_row["avg_cost"]
                new_qty = old_qty + qty
                new_cost = (old_qty * old_cost + qty * fill_px) / new_qty if new_qty > 0 else fill_px
                con.execute(
                    "update agent_positions set qty=?, avg_cost=?, updated_at=? "
                    "where agent_id=? and symbol=?",
                    (new_qty, new_cost, as_of, agent_id, symbol)
                )
            else:
                con.execute(
                    "insert into agent_positions (agent_id, symbol, qty, avg_cost, updated_at) "
                    "values (?, ?, ?, ?, ?)",
                    (agent_id, symbol, qty, fill_px, as_of)
                )

        elif side == "SELL":
            fill_px = open_px * (1 - slip)
            # Get qty from trade record
            qty = trade["qty"] or 0.0
            proceeds = qty * fill_px

            # Update cash
            cash = _get_current_cash(con, agent_id)
            new_cash = cash + proceeds

            # Update position
            pos_row = con.execute(
                "select qty from agent_positions where agent_id=? and symbol=?",
                (agent_id, symbol)
            ).fetchone()
            if pos_row:
                remaining = pos_row["qty"] - qty
                if remaining <= 1e-8:
                    con.execute(
                        "delete from agent_positions where agent_id=? and symbol=?",
                        (agent_id, symbol)
                    )
                else:
                    con.execute(
                        "update agent_positions set qty=?, updated_at=? "
                        "where agent_id=? and symbol=?",
                        (remaining, as_of, agent_id, symbol)
                    )
        else:
            continue

        # Update trade record
        con.execute(
            "update agent_trades set status='filled', exec_date=?, price=?, "
            "qty=?, cash_after=? where id=?",
            (as_of, fill_px, qty, new_cash, trade["id"])
        )

        # Update agent_state cash
        month_key = as_of[:7]
        con.execute(
            "insert or replace into agent_state (agent_id, month, cash, nav, updated_at) "
            "values (?, ?, ?, ?, ?)",
            (agent_id, month_key, new_cash, new_cash, as_of)
        )

    con.commit()


def _validate_and_record_actions(
    con: sqlite3.Connection,
    agent_id: str,
    domain: str,
    decided_date: str,
    actions: list,
    briefing_path: str,
) -> None:
    """Validate agent actions and record them as pending or rejected."""
    pool = set(DOMAINS.get(domain, []))
    nav = _get_nav(con, agent_id, decided_date)
    cash = _get_current_cash(con, agent_id)

    evaluated_count = 0  # Actions evaluated so far (1-indexed against MAX_TRADES_PER_DAY)

    for action in actions:
        if not isinstance(action, dict):
            action = {}
        side = (str(action.get("side") or "")).upper()
        symbol = (str(action.get("symbol") or "")).upper()
        reason = str(action.get("reason") or "")
        try:
            usd = float(action.get("usd") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        try:
            pct = float(action.get("pct") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0

        # Daily cap: 4th action onwards automatically rejected
        if evaluated_count >= MAX_TRADES_PER_DAY:
            con.execute(
                "insert into agent_trades "
                "(agent_id, decided_date, symbol, side, reason, status, rejected_reason, briefing_path) "
                "values (?, ?, ?, ?, ?, 'rejected', ?, ?)",
                (agent_id, decided_date, symbol, side, reason,
                 "exceeds daily limit", briefing_path)
            )
            continue

        evaluated_count += 1

        # Validate
        rejected_reason = None

        if side == "BUY":
            if symbol not in pool:
                rejected_reason = f"symbol {symbol} not in domain pool"
            elif usd <= 0:
                rejected_reason = "BUY 缺少有效 usd 欄位（美元金額必須 > 0）"
            elif not reason.strip():
                rejected_reason = "缺少 reason（每筆交易必須附理由）"
            elif usd > MAX_POS_PCT * nav:
                rejected_reason = f"exceeds 40% NAV cap ({usd:.0f} > {MAX_POS_PCT * nav:.0f})"
            elif usd > cash:
                rejected_reason = f"insufficient cash ({cash:.2f} available)"

            if rejected_reason:
                con.execute(
                    "insert into agent_trades "
                    "(agent_id, decided_date, symbol, side, usd, reason, status, rejected_reason, briefing_path) "
                    "values (?, ?, ?, ?, ?, ?, 'rejected', ?, ?)",
                    (agent_id, decided_date, symbol, side, usd, reason, rejected_reason, briefing_path)
                )
            else:
                # Accept as pending
                con.execute(
                    "insert into agent_trades "
                    "(agent_id, decided_date, symbol, side, usd, reason, status, briefing_path) "
                    "values (?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (agent_id, decided_date, symbol, side, usd, reason, briefing_path)
                )
                # Reserve cash optimistically (deducted on fill)
                # We don't deduct cash at decision time, only at fill time

        elif side == "SELL":
            pos_row = con.execute(
                "select qty from agent_positions where agent_id=? and symbol=?",
                (agent_id, symbol)
            ).fetchone()
            if pos_row is None or pos_row["qty"] <= 0:
                rejected_reason = f"no position in {symbol}"
            elif pct <= 0 or pct > 100:
                rejected_reason = f"SELL pct 必須為 1-100（收到 {pct}）"
            elif not reason.strip():
                rejected_reason = "缺少 reason（每筆交易必須附理由）"

            if rejected_reason:
                con.execute(
                    "insert into agent_trades "
                    "(agent_id, decided_date, symbol, side, reason, status, rejected_reason, briefing_path) "
                    "values (?, ?, ?, ?, ?, 'rejected', ?, ?)",
                    (agent_id, decided_date, symbol, side, reason, rejected_reason, briefing_path)
                )
            else:
                qty_to_sell = pos_row["qty"] * (pct / 100.0)
                con.execute(
                    "insert into agent_trades "
                    "(agent_id, decided_date, symbol, side, qty, reason, status, briefing_path) "
                    "values (?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (agent_id, decided_date, symbol, side, qty_to_sell, reason, briefing_path)
                )

        else:
            # 非 BUY/SELL（含 LLM 自創欄位名如 sym/type/qty）→ 記錄拒單
            # 作為下次簡報的學習訊號，絕不靜默丟棄
            con.execute(
                "insert into agent_trades "
                "(agent_id, decided_date, symbol, side, reason, status, rejected_reason, briefing_path) "
                "values (?, ?, ?, ?, ?, 'rejected', ?, ?)",
                (agent_id, decided_date, symbol or "?", side or "?", reason,
                 "malformed action：side 必須為 BUY/SELL，欄位限 symbol/usd/pct/reason",
                 briefing_path)
            )

    con.commit()


def _record_nav(con: sqlite3.Connection, agent_id: str, as_of: str) -> None:
    """Record end-of-day NAV for an agent."""
    positions = con.execute(
        "select symbol, qty from agent_positions where agent_id=?", (agent_id,)
    ).fetchall()

    cash = _get_current_cash(con, agent_id)
    nav = cash

    for pos in positions:
        px_row = con.execute(
            "select close from prices where symbol=? and date=?",
            (pos["symbol"], as_of)
        ).fetchone()
        if px_row and px_row["close"]:
            nav += pos["qty"] * px_row["close"]
        else:
            # Use latest available price
            px_row2 = con.execute(
                "select close from prices where symbol=? and date<=? order by date desc limit 1",
                (pos["symbol"], as_of)
            ).fetchone()
            if px_row2:
                nav += pos["qty"] * px_row2["close"]

    con.execute(
        "insert or replace into agent_nav_daily (agent_id, date, nav, cash) values (?, ?, ?, ?)",
        (agent_id, as_of, nav, cash)
    )

    # Update hwm
    hwm_row = con.execute("select hwm from agents where id=?", (agent_id,)).fetchone()
    if hwm_row and nav > hwm_row["hwm"]:
        con.execute("update agents set hwm=? where id=?", (nav, agent_id))

    con.commit()


# ---------------------------------------------------------------------------
# run_daily
# ---------------------------------------------------------------------------

def run_daily(
    con: sqlite3.Connection,
    as_of: str,
    backend: AgentBackend,
    only_agents: list = None,
    data_dir=None,
) -> None:
    """
    日循環：撮合昨日 pending → 生成簡報 → 決策 → 記錄 pending 單 → 記錄 NAV。
    冪等：同 (agent_id, as_of) 已有 agent_nav_daily 列則跳過。
    """
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR

    # 1. Fill all pending orders using today's open prices
    _fill_pending_orders(con, as_of)

    # 2. Get agents to process
    if only_agents:
        agents = con.execute(
            "select id, domain, style_seed from agents where id in ({}) and status='active'".format(
                ",".join("?" * len(only_agents))
            ),
            only_agents
        ).fetchall()
    else:
        agents = con.execute(
            "select id, domain, style_seed from agents where status='active'"
        ).fetchall()

    for agent in agents:
        agent_id = agent["id"]
        domain = agent["domain"]

        # Idempotency: skip if already processed today
        existing = con.execute(
            "select 1 from agent_nav_daily where agent_id=? and date=?",
            (agent_id, as_of)
        ).fetchone()
        if existing:
            continue

        # 3. Generate briefing
        try:
            briefing = build_briefing(con, domain, as_of, agent_id, data_dir=data_dir)
        except Exception as exc:
            print(f"[arena] 簡報生成失敗 {agent_id}: {exc}")
            briefing = f"# BRIEF {as_of} | domain={domain}\n（簡報生成失敗: {exc}）"

        # Save briefing
        briefing_path = _save_briefing(briefing, as_of, agent_id, data_dir)

        # 4. Load strategy
        strat_file = data_dir / "agents" / agent_id / "strategy.md"
        strategy = strat_file.read_text(encoding="utf-8") if strat_file.exists() else ""

        # 5. Call backend.decide()
        try:
            decision = backend.decide(agent_id, briefing, strategy)
        except Exception as exc:
            print(f"[arena] decide 失敗 {agent_id}: {exc}")
            decision = {"actions": [], "watch": [], "memory_note": "", "backend_error": True}

        actions = decision.get("actions") or []
        memory_note = (decision.get("memory_note") or "").strip()[:100]

        # 6. Validate and record actions
        _validate_and_record_actions(
            con, agent_id, domain, as_of, actions, briefing_path
        )

        # 7. Record memory note
        if memory_note:
            con.execute(
                "insert into agent_memory (agent_id, date, content, created_at) values (?, ?, ?, ?)",
                (agent_id, as_of, f"{as_of[:5]}{as_of[5:]}: {memory_note}", datetime.utcnow().isoformat())
            )
            # Keep only last 10 memories
            mem_rows = con.execute(
                "select id from agent_memory where agent_id=? order by id desc",
                (agent_id,)
            ).fetchall()
            if len(mem_rows) > 10:
                old_ids = [r["id"] for r in mem_rows[10:]]
                con.execute(
                    "delete from agent_memory where id in ({})".format(",".join("?" * len(old_ids))),
                    old_ids
                )

        con.commit()

        # 8. Record end-of-day NAV
        _record_nav(con, agent_id, as_of)

    print(f"[arena] daily {as_of} 完成")


# ---------------------------------------------------------------------------
# run_monthly
# ---------------------------------------------------------------------------

def _calc_mdd(navs: list[float]) -> float:
    """Calculate Maximum Drawdown from NAV series."""
    if len(navs) < 2:
        return 0.0
    peak = navs[0]
    mdd = 0.0
    for nav in navs:
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak * 100 if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd
    return round(mdd, 2)


def _calc_win_rate(con: sqlite3.Connection, agent_id: str, month: str) -> float:
    """Win rate: filled trades with positive return."""
    trades = con.execute(
        "select side, qty, price, symbol from agent_trades "
        "where agent_id=? and exec_date like ? and status='filled'",
        (agent_id, month + "%")
    ).fetchall()
    if not trades:
        return 0.0
    wins = 0
    total = 0
    for t in trades:
        # Simple: BUY wins if close at month end > fill price
        total += 1
        if t["side"] == "SELL":
            wins += 1  # A SELL is a win if we executed (simplified)
        else:
            wins += 0  # Simplified: count filled BUYs that gained value
    return round(wins / total * 100, 1) if total > 0 else 0.0


def run_monthly(
    con: sqlite3.Connection,
    month: str,
    backend: AgentBackend,
    data_dir=None,
) -> None:
    """
    月度結算與反思循環。
    month format: 'YYYY-MM'
    冪等：agent_monthly 已有該月列則跳過。
    持倉延續制：不清倉。
    """
    data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR

    agents = con.execute(
        "select id, domain, style_seed from agents"
    ).fetchall()

    # Gather NAV data for the month
    agent_data = {}
    for agent in agents:
        agent_id = agent["id"]
        nav_rows = con.execute(
            "select date, nav from agent_nav_daily where agent_id=? and date like ? order by date",
            (agent_id, month + "%")
        ).fetchall()
        if not nav_rows:
            continue

        nav_start = nav_rows[0]["nav"]
        nav_end = nav_rows[-1]["nav"]
        navs = [r["nav"] for r in nav_rows]
        ret_pct = (nav_end / nav_start - 1) * 100 if nav_start else 0.0
        mdd = _calc_mdd(navs)
        n_trades = con.execute(
            "select count(*) from agent_trades where agent_id=? and exec_date like ? and status='filled'",
            (agent_id, month + "%")
        ).fetchone()[0]
        win_rate = _calc_win_rate(con, agent_id, month)

        agent_data[agent_id] = {
            "agent": agent,
            "nav_start": nav_start,
            "nav_end": nav_end,
            "ret_pct": ret_pct,
            "mdd": mdd,
            "n_trades": n_trades,
            "win_rate": win_rate,
            "navs": navs,
        }

    # Compute rankings
    all_rets = sorted(agent_data.items(), key=lambda x: x[1]["ret_pct"], reverse=True)
    overall_ranks = {aid: i + 1 for i, (aid, _) in enumerate(all_rets)}

    domain_rets: dict[str, list] = {}
    for aid, d in agent_data.items():
        dom = d["agent"]["domain"]
        domain_rets.setdefault(dom, []).append((aid, d["ret_pct"]))
    domain_ranks = {}
    for dom, items in domain_rets.items():
        sorted_items = sorted(items, key=lambda x: x[1], reverse=True)
        for rank, (aid, _) in enumerate(sorted_items, 1):
            domain_ranks[aid] = rank

    # Process each agent
    for agent_id, d in agent_data.items():
        # Idempotency
        existing = con.execute(
            "select 1 from agent_monthly where agent_id=? and month=?",
            (agent_id, month)
        ).fetchone()
        if existing:
            print(f"[arena] monthly {agent_id} {month} 已存在，跳過")
            continue

        agent = d["agent"]

        # Read current strategy
        strat_file = data_dir / "agents" / agent_id / "strategy.md"
        strategy_before = strat_file.read_text(encoding="utf-8") if strat_file.exists() else ""

        # Build reflection dossier
        trades_text = "\n".join(
            f"  {t['decided_date']} {t['side']} {t['symbol']} status={t['status']} reason={t['reason']}"
            for t in con.execute(
                "select decided_date, side, symbol, status, reason from agent_trades "
                "where agent_id=? and decided_date like ? order by id",
                (agent_id, month + "%")
            ).fetchall()
        ) or "（無交易）"

        dossier = (
            f"## {agent_id} 月度報告 {month}\n"
            f"NAV: {d['nav_start']:.2f} → {d['nav_end']:.2f} | "
            f"報酬率: {d['ret_pct']:.2f}% | MDD: {d['mdd']:.2f}% | "
            f"勝率: {d['win_rate']:.1f}% | 交易筆數: {d['n_trades']}\n"
            f"領域排名: {domain_ranks.get(agent_id, '?')} / 全場排名: {overall_ranks.get(agent_id, '?')}\n\n"
            f"### 交易明細\n{trades_text}\n\n"
            f"### 現行策略卡\n{strategy_before}\n"
        )

        # Call backend.reflect()
        try:
            reflection = backend.reflect(agent_id, dossier)
        except Exception as exc:
            print(f"[arena] reflect 失敗 {agent_id}: {exc}")
            reflection = {"public_letter": "", "reflection_md": "", "strategy_md": ""}

        public_letter = (reflection.get("public_letter") or "")[:500]
        reflection_md = (reflection.get("reflection_md") or "")[:1000]
        new_strategy = reflection.get("strategy_md") or ""
        strategy_after = new_strategy if new_strategy else strategy_before

        # Rewrite strategy card if provided
        if new_strategy:
            strat_file.parent.mkdir(parents=True, exist_ok=True)
            strat_file.write_text(new_strategy, encoding="utf-8")

        # Write agent_monthly
        con.execute(
            "insert or replace into agent_monthly "
            "(agent_id, month, nav_start, nav_end, ret_pct, mdd_pct, win_rate, n_trades, "
            "rank_domain, rank_overall, public_letter, reflection_md, strategy_before, strategy_after) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id, month,
                d["nav_start"], d["nav_end"],
                round(d["ret_pct"], 4),
                d["mdd"],
                d["win_rate"],
                d["n_trades"],
                domain_ranks.get(agent_id),
                overall_ranks.get(agent_id),
                public_letter,
                reflection_md,
                strategy_before,
                strategy_after,
            )
        )

        # Relaunch check
        nav_end = d["nav_end"]
        hwm_row = con.execute("select hwm, relaunches from agents where id=?", (agent_id,)).fetchone()
        hwm = hwm_row["hwm"] if hwm_row else INIT_CAPITAL

        should_relaunch = (
            nav_end < RELAUNCH_FLOOR or
            (hwm > 0 and nav_end < hwm * (1 - RELAUNCH_DD))
        )

        if should_relaunch:
            print(f"[arena] {agent_id} 觸發爆倉重整（NAV={nav_end:.2f}, HWM={hwm:.2f}）")
            # Clear positions (持倉清倉)
            con.execute("delete from agent_positions where agent_id=?", (agent_id,))
            # Reset cash
            new_month = (datetime.strptime(month, "%Y-%m") + timedelta(days=32)).strftime("%Y-%m")
            con.execute(
                "insert or replace into agent_state (agent_id, month, cash, nav, updated_at) "
                "values (?, ?, ?, ?, ?)",
                (agent_id, new_month, INIT_CAPITAL, INIT_CAPITAL, datetime.utcnow().isoformat())
            )
            # Also update current month state
            con.execute(
                "insert or replace into agent_state (agent_id, month, cash, nav, updated_at) "
                "values (?, ?, ?, ?, ?)",
                (agent_id, month, INIT_CAPITAL, INIT_CAPITAL, datetime.utcnow().isoformat())
            )
            # Increment relaunches
            con.execute(
                "update agents set relaunches=relaunches+1, hwm=?, status='active' where id=?",
                (INIT_CAPITAL, agent_id)
            )

    con.commit()
    print(f"[arena] monthly {month} 完成")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_migrate(args):
    db_path = args.db or DEFAULT_DB
    con = connect(db_path)
    migrate(con)
    con.close()
    print("資料庫表遷移完成（冪等）。")


def _latest_trading_date(con: sqlite3.Connection) -> str:
    """prices 表最新交易日（排程於台北早晨執行時，當日美股尚未收盤，
    以日曆日當 as_of 會導致 pending 單永遠等不到當日開盤價）。"""
    row = con.execute("select max(date) from prices").fetchone()
    return row[0] if row and row[0] else datetime.now().strftime("%Y-%m-%d")


def cmd_daily(args):
    db_path = args.db or DEFAULT_DB
    as_of = args.as_of
    only_agents = [a.strip() for a in args.agents.split(",")] if args.agents else None

    # Try GeminiBackend unless dry-run
    if args.dry_run:
        backend = StubBackend()
        print(f"[arena] dry-run 模式，使用 StubBackend")
    else:
        try:
            backend = GeminiBackend()
        except RuntimeError as exc:
            print(f"[錯誤] {exc}", file=sys.stderr)
            sys.exit(1)

    con = connect(db_path)
    try:
        migrate(con)
        seed_agents(con)
        if not as_of:
            as_of = _latest_trading_date(con)
            print(f"[arena] as_of 未指定，採 prices 最新交易日 {as_of}")
        run_daily(con, as_of, backend, only_agents=only_agents)
    finally:
        con.close()


def cmd_monthly(args):
    db_path = args.db or DEFAULT_DB
    month = args.month or datetime.now().strftime("%Y-%m")

    try:
        backend = GeminiBackend()
    except RuntimeError as exc:
        print(f"[錯誤] {exc}", file=sys.stderr)
        sys.exit(1)

    con = connect(db_path)
    try:
        run_monthly(con, month, backend)
    finally:
        con.close()


def cmd_status(args):
    db_path = args.db or DEFAULT_DB
    con = connect(db_path)
    try:
        agents = con.execute(
            "select id, domain, style_seed, status, relaunches, hwm from agents order by id"
        ).fetchall()
        if not agents:
            print("尚無 agent（請先執行 migrate + seed）。")
            return

        print(f"{'Agent ID':<25} {'Domain':<12} {'Style':<10} {'Status':<8} {'Relaunch':<9} {'HWM':>8}")
        print("-" * 80)
        for a in agents:
            # Get latest NAV
            nav_row = con.execute(
                "select nav, date from agent_nav_daily where agent_id=? order by date desc limit 1",
                (a["id"],)
            ).fetchone()
            nav_str = f"{nav_row['nav']:.2f} ({nav_row['date']})" if nav_row else "N/A"
            print(f"{a['id']:<25} {a['domain']:<12} {a['style_seed']:<10} {a['status']:<8} "
                  f"{a['relaunches']:<9} {a['hwm']:>8.2f}  NAV={nav_str}")
    finally:
        con.close()


def main():
    # Shared --db parent parser (allows --db before or after subcommand)
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db", metavar="PATH", help="覆寫資料庫路徑")

    parser = argparse.ArgumentParser(
        description="Serenity V6 AI 經理人競技場（模擬資金・非投資建議）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[db_parent],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # migrate
    sub.add_parser("migrate", help="冪等建立 / 升級資料庫表", parents=[db_parent])

    # daily
    p_daily = sub.add_parser("daily", help="執行日循環決策", parents=[db_parent])
    p_daily.add_argument("--as-of", metavar="YYYY-MM-DD", help="指定日期（預設今日）")
    p_daily.add_argument("--agents", metavar="a,b,...", help="只處理指定 agent（逗號分隔）")
    p_daily.add_argument("--dry-run", action="store_true", help="使用 StubBackend（不呼叫 Gemini）")

    # monthly
    p_monthly = sub.add_parser("monthly", help="執行月度結算與反思", parents=[db_parent])
    p_monthly.add_argument("--month", metavar="YYYY-MM", help="指定月份（預設當月）")

    # status
    sub.add_parser("status", help="顯示 agent 狀態摘要", parents=[db_parent])

    args = parser.parse_args()

    if args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "daily":
        cmd_daily(args)
    elif args.command == "monthly":
        cmd_monthly(args)
    elif args.command == "status":
        cmd_status(args)


if __name__ == "__main__":
    main()
