# Serenity Signal V6 — AI 經理人競技場（Fund Agent Arena）需求規格

> 版本：v1.0 | 建立：2026-07-06 | 整理者：Fable（監督者）
> 目標：建立多領域、多 agent 的**模擬**選股經理人系統。每個 agent
> 管理一筆虛擬資金，每日讀取精簡市場簡報做出買賣決策，月底結算
> 績效、互相學習、自我迭代策略。
>
> **鐵律聲明：本系統為 paper trading（虛擬資金 + 真實市場價格），
> 不連接任何券商、不執行任何真實交易。** 所有「$3,000 資金」均為
> 模擬帳上數字。

---

## R6-0 核心設計原則

1. **零捏造**：agent 只能交易「當日簡報中出現的股票」，成交價
   一律取自 `prices` 表真實列；引擎驗證每筆交易的價格與現金流。
2. **零 look-ahead**：日 T 收盤後生成簡報（僅含 ≤T 的資料），
   決策於 T+1 開盤價成交（含滑價模型，沿用 B-3 成本建模結論）。
3. **每筆交易可稽核**：金額、股數、成交價、**購買理由（agent 原文）**、
   當日簡報快照全部落地，可完整重放（replay）任一天的決策情境。
4. **精簡輸入**：每次決策的輸入 token 預算 ≤ 4K（簡報 ~2K +
   策略卡 ~0.6K + 記憶 ~0.8K + 指令 ~0.6K），輸出強制 JSON。
5. **可自我迭代**：閱讀格式（format_prefs）與買賣邏輯（strategy card）
   都是 agent 自己的資產，月底反思時由 agent 自行改寫（在白名單
   約束內），版本全留存。
6. **後端可抽換**：LLM 呼叫走抽象層，v1 用 Gemini（既有 KeyManager
   四鑰池），預留 Antigravity CLI / 其他 API 的 adapter 介面。

---

## R6-1 競技場結構

### 領域與 agent 編制

| 領域 | 代號 | 股票池（從既有 82 檔挑選） | agents |
|------|------|---------------------------|--------|
| 半導體 | `semis` | NVDA TSM AVGO AMD ASML MU AMAT INTC ARM MRVL ON STM TER GFS COHR LITE ALAB SIMO | 3 |
| 機器人/自動化 | `robotics` | TSLA TER AEVA RKLB ASTS PL（現有池偏少，**需擴充**：建議加 ISRG SYM PATH ZBRA，走 `ingest.py prices` 增量納入） | 3 |
| AI 雲端/軟體（可選第三領域） | `ai_cloud` | MSFT GOOGL META AMZN ORCL CRM NBIS CRWV VRT | 3 |

- v1 先開 `semis` + `robotics` 兩領域共 6 個 agent；`ai_cloud` 留為
  Phase 4 擴充位。
- 每個領域 3 個 agent 以**不同初始風格**啟動（製造多樣性，之後
  允許漂移）：
  - **A 動能型**：追趨勢、順勢加倉、破位停損
  - **B 逢低型**：均值回歸、拉回買進、分批建倉
  - **C 事件型**：新聞/財報/13F 催化劑驅動
- agent 命名：`semis-momentum`、`semis-dip`、`semis-catalyst`、
  `robotics-momentum` … 依此類推。

### 資金與結算規則

> 設計決策（2026-07-06）：採**持倉延續制**，不做每月清倉。
> 理由：專業基金皆為連續投資組合 + 定期 mark-to-market 考績；
> 每月強制清倉會讓所有 agent 退化成單月短線風格，殺死逢低型
> 與事件型策略的跨月論點，損失多樣性。月度排名用報酬率 %，
> 百分比天然歸一化，資金規模不影響公平性。

- 每個 agent 成立時獲得**一次性**虛擬 **$3,000** 本金，此後
  持倉與現金跨月延續（複利滾動），不再逐月注資。
- 月底最後一個交易日收盤 mark-to-market 結算：
  NAV = 現金 + Σ(持股 × 收盤價)，**含未實現損益**——持有不動
  也是決策，帳面盈虧照樣計入考績。
- 雙軌排名：
  - **月度考績**：當月報酬率 %（NAV 月變動），領域內 + 全場排名
  - **生涯榜**：成立以來累計報酬率——長線論點的價值在這裡體現
- **爆倉重整條款**（參考多策略平台的回撤紀律）：NAV 自
  高水位（high-water mark）回撤 ≥40%，或絕對值跌破 $1,500 →
  強制清倉 + 深度反思（必須逐筆檢討虧損交易）+ 策略卡大改
  → 重新注資 $3,000 再出發。重整次數（relaunches）永久記在
  生涯榜上，是誠實的履歷汙點。
- 交易規則：
  - 允許零股（fractional shares，$3,000 才玩得動 NVDA）
  - 不可放空、不可槓桿（v1）
  - 每日最多 3 筆交易；單一持股上限 NAV 的 40%
  - 成交價 = T+1 開盤價 × (1 ± 滑價 5bps)（買加賣減）
  - 手續費模擬：每筆 $0（美股零佣），滑價即隱含成本
- 排名：領域內排名 + 全場排名，指標為當月報酬率 %；
  同場亦記錄最大回撤（MDD）與勝率供反思用。

---

## R6-2 精簡簡報格式（Briefing v1）

每個交易日收盤後，per-domain 生成一份純文字簡 報（所有欄位
可追溯 SQLite），目標 ~1.5-2K tokens：

```
# BRIEF 2026-07-06 | domain=semis | regime=BULL (SPY>EMA200, breadth 62%)

## PRICES  (sym  close  chg1d  chg5d  chg20d  rsi14  ema50  vol_z)
NVDA  172.40  +1.2%  +4.5%  +11.2%  68  ↑  +0.8
TSM   211.00  -0.5%  +2.1%   +8.9%  61  ↑  -0.2
...（domain 股票池全列，一檔一行）

## NEWS  (sym|hrs_ago|title — 最多 10 則，僅標題)
NVDA|6|Nvidia unveils next-gen Rubin platform at ...
MACRO|12|Fed holds rates steady, signals ...

## ESTIMATES_CHANGES  (僅列本週有變動者：sym tgt_mean vs_px rev n)
NVDA  195  +13%  ↑  42

## EXPERT_VIEWS  (僅列新增之 13F 異動，含 45 天延遲警語)
Berkshire 13F Q1: 減倉 CHEVRON -12% [filed 45d-delay]

## EARNINGS_SOON  (≤14 天內公布財報者)
AVGO 2026-07-10 (4d)

## YOUR_PORTFOLIO  (cash=1240.50 | nav=3105.20 | mtd=+3.5%)
NVDA  5.2sh  @165.20  now 172.40  P/L +4.4%  (28% of NAV)

## YOUR_MEMORY_DIGEST  (你上次留下的備忘，≤3 行)
7/03: AVGO 財報前不追高，等回檔至 EMA20。
```

### 格式自我迭代（format_prefs）

每個 agent 有 `format_prefs.json`，在**白名單 schema** 內決定
自己想讀什麼（防止 prompt injection 與 token 爆量）：

```json
{
  "news_count": 10,          // 0-15
  "show_estimates": true,
  "show_expert_views": true,
  "show_earnings": true,
  "price_columns": ["chg1d","chg5d","chg20d","rsi14","ema50","vol_z"],
                             // 從固定欄位清單勾選
  "extra_symbols": []        // 最多 3 檔池外觀察股（僅價格行）
}
```

月底反思時 agent 可修改 prefs；引擎驗證 schema，非法值回退預設。

---

## R6-3 決策輸出契約（強制 JSON）

```json
{
  "actions": [
    {"side": "BUY",  "symbol": "NVDA", "usd": 600,  "reason": "突破 20 日高點且 vol_z>0.5，動能延續；規則：突破加倉 20%"},
    {"side": "SELL", "symbol": "MU",   "pct": 100,  "reason": "跌破 EMA50 觸發停損規則"}
  ],
  "watch": ["AVGO"],
  "memory_note": "AVGO 財報 7/10，前 2 日若 RSI>70 不進場"
}
```

- `actions` 可為空陣列（持有不動也是決策，照樣記錄）
- BUY 用 `usd`（引擎換算零股股數）、SELL 用 `pct`（持倉百分比）
- `reason` 每筆必填，≤100 字，**原文入庫**
- `memory_note` ≤50 字，寫入 agent 記憶（滾動保留最近 10 條 +
  月度反思摘要，構成下次簡報的 MEMORY_DIGEST）
- 引擎驗證：symbol 必須在簡報內、現金足夠、不超過持股上限、
  一日 ≤3 筆；違規 action 整筆拒絕並記入 `rejected_reason`
  （agent 下次會在簡報看到自己被拒的原因——這也是學習訊號）

---

## R6-4 策略卡與月度反思（自我迭代 + 互相學習）

### 策略卡（strategy card）

`agents/<id>/strategy.md`，≤600 tokens，agent 的「操作手冊」，
初始由監督者按三種風格寫 v1，之後**每月由 agent 自己改寫**：

```markdown
# semis-momentum 策略卡 v3 (2026-09-01)
## 進場
- 突破 20 日高 + vol_z > 0.5 → 首倉 NAV 15%
- ...
## 出場
- 收盤跌破 EMA50 → 全出
## 倉位
- 單股上限 35%（低於系統上限 40%，自留緩衝）
## 本月修正（來自 8 月反思）
- 財報前 3 日不建新倉（8 月兩次財報跳空虧損）
```

### 月度結算 → 反思 → 學習循環

月底最後交易日收盤後依序執行：

1. **結算**：計算各 agent NAV、報酬率、MDD、勝率、交易次數，
   寫入 `agent_monthly(month, agent_id, nav_end, ret_pct, mdd, ...)`
2. **公開信**：每個 agent 先寫一封 ≤200 字「致同行公開信」——
   本月最有效的一招 + 最痛的一次虧損（誠實聲明：引擎附上
   該 agent 真實交易記錄摘要，防吹牛）
3. **反思**（每 agent 一次 LLM 呼叫，用較強模型）輸入：
   - 自己整月交易明細（含被拒單）+ 績效指標
   - 領域排行榜 + **全部同領域 agent 的公開信**（互相學習管道）
   - 自己現行策略卡 + format_prefs
   輸出：
   - `reflection_md`（≤500 字檢討）
   - 新版 `strategy.md`（全文改寫，引擎存版本鏈）
   - `format_prefs` 修改（可選）
4. **持倉檢視（不清倉）**：反思時引擎附上現有持倉明細
   （成本、現價、未實現損益、持有天數），agent 必須對每筆
   跨月持倉表態「續抱理由」或「列入下月出場計畫」——寫入
   記憶摘要跨月傳承。像真實經理人月會：檢討組合，而非拆掉重建。
5. **爆倉重整檢查**：觸發 R6-1 重整條款者，執行清倉歸檔 +
   深度反思 + 重新注資，`agents.relaunches` 加一。

### 學習的邊界（防止同質化）

- agent 只能看到別人的**公開信與排名**，看不到別人的策略卡全文
  與交易明細——像真實基金界：知道誰贏、聽其談論，但抄不到
  完整配方。保持策略多樣性。

---

## R6-5 資料庫 schema（新增六表，冪等遷移）

```sql
create table if not exists agents(
  id text primary key,            -- 'semis-momentum'
  domain text not null,
  style_seed text not null,       -- 'momentum'|'dip'|'catalyst'
  backend text not null default 'gemini',  -- 'gemini'|'antigravity'|...
  status text not null default 'active',
  relaunches integer not null default 0,   -- 爆倉重整次數（生涯履歷）
  hwm real not null default 3000,          -- NAV 高水位（重整條款用）
  created_at text not null
);
create table if not exists agent_state(
  agent_id text, month text,      -- '2026-07'
  cash real, nav real, updated_at text,
  primary key(agent_id, month)
);
create table if not exists agent_positions(
  agent_id text, symbol text, qty real, avg_cost real, updated_at text,
  primary key(agent_id, symbol)
);
create table if not exists agent_trades(
  id integer primary key autoincrement,
  agent_id text not null,
  decided_date text not null,     -- 簡報日 T
  exec_date text,                 -- 成交日 T+1（拒單為 null）
  symbol text, side text, qty real, price real, usd real,
  cash_after real,
  reason text not null,           -- agent 原文
  status text not null,           -- 'filled'|'rejected'
  rejected_reason text,
  briefing_path text not null     -- data/briefings/2026-07-06_semis.txt
);
create table if not exists agent_monthly(
  agent_id text, month text,
  nav_start real, nav_end real, ret_pct real,
  mdd_pct real, win_rate real, n_trades integer,
  rank_domain integer, rank_overall integer,
  public_letter text, reflection_md text,
  strategy_before text, strategy_after text,
  primary key(agent_id, month)
);
create table if not exists agent_nav_daily(
  agent_id text, date text, nav real, cash real,
  primary key(agent_id, date)
);
```

簡報快照存純文字檔 `data/briefings/`（gitignore），路徑入
`agent_trades.briefing_path` 供重放稽核。

---

## R6-6 LLM 後端抽象層

```python
class AgentBackend(ABC):
    def decide(self, briefing: str, strategy: str, instructions: str) -> dict: ...
    def reflect(self, dossier: str, instructions: str) -> dict: ...

class GeminiBackend(AgentBackend):
    # 走既有 KeyManager，task_class="agent_arena"
    # 日常決策：gemini-2.5-flash（6 agents × 1 call/日 = 6 calls/日）
    # 月度反思：gemini-2.5-pro 或 flash-thinking（6 calls/月）

class AntigravityBackend(AgentBackend):
    # 預留：shell out 至 antigravity CLI agent，stdin 餵簡報、
    # stdout 收 JSON；v1 只定義介面 + NotImplementedError
    # 未來亦可比較「同策略卡、不同 backend」的績效差異
```

- 失敗處理：LLM 呼叫失敗/JSON 解析失敗 → 重試 1 次 → 仍失敗則
  當日記錄 `HOLD (backend_error)`，不影響其他 agent，不中斷管線。
- Token 用量落地 `data/chat_monitor.json` 既有監控（成本可視）。

---

## R6-7 排程整合（新增 J-10/J-11）

| 任務 | 指令 | 頻率 |
|------|------|------|
| J-10 每日決策循環 | `python scripts/agent_arena.py daily` | 每日 08:00（J-1~J-8 之後，確保簡報資料新鮮） |
| J-11 月度結算+反思 | `python scripts/agent_arena.py monthly` | 每月 1 日 08:30 |

`daily` 內部流程：先撮合昨日 pending 單（用今日開盤價）→
生成今日簡報 → 逐 agent 決策 → 寫入 pending 單 → 記錄當日 NAV。
冪等：同日重跑自動跳過已完成步驟。

---

## R6-8 前端（新全域頁「🤖 經理人競技場」）

- **排行榜**：雙軌呈現——月度考績（當月報酬率、MDD、交易數，
  領域分組，冠軍徽章）+ 生涯榜（累計報酬率、重整次數）；
  歷月成績可切換
- **NAV 曲線**：6 條曲線疊圖（Lightweight Charts 既有依賴），
  對照 SPY/SOXX 基準線
- **交易日誌**：時間軸列出每筆交易——日期、標的、金額、
  **購買理由原文**、成交/拒單狀態；可按 agent 篩選
- **反思檔案室**：每月每 agent 的公開信 + 反思全文 + 策略卡
  版本 diff（讀 `agent_monthly`）
- 明顯處標示：「**模擬資金・非投資建議**」

API 契約：

```
GET /api/arena/leaderboard?month=2026-07
  → {"month", "rows":[{agent_id, domain, ret_pct, mdd_pct, n_trades, rank_domain, rank_overall}]}
GET /api/arena/nav?month=2026-07
  → {"series":{"semis-momentum":[{date,nav},...], ...}, "benchmark":{"SPY":[...]}}
GET /api/arena/trades?agent=semis-momentum&month=2026-07
  → {"trades":[{decided_date, exec_date, symbol, side, qty, price, usd, reason, status}]}
GET /api/arena/reflections?month=2026-07
  → {"rows":[{agent_id, public_letter, reflection_md, strategy_after}]}
```

---

## R6-9 實作步驟（四個 Phase）

| Phase | 內容 | 驗收 |
|-------|------|------|
| **P1 引擎骨架** | schema 遷移、簡報生成器（semis 領域）、交易引擎（驗證/撮合/NAV）、GeminiBackend、單一 agent 跑通一日循環 | dry-run 一天：簡報落地、決策 JSON 合法、成交記錄可稽核 |
| **P2 六 agent 日循環** | 6 agents（2 領域 × 3 風格）策略卡 v1、robotics 股票池擴充納入 ingest、J-10 排程、拒單回饋 | 連跑 5 個交易日無人工介入；`agent_nav_daily` 6 條曲線正常 |
| **P3 月度閉環** | 結算、公開信、反思、策略卡改寫、format_prefs 迭代、月初重置、J-11 排程 | 模擬跑一次完整月結（可用歷史回放模式壓縮測試） |
| **P4 前端+擴充** | 競技場頁四區塊、API 四端點、`ai_cloud` 第三領域、AntigravityBackend adapter 介面定稿 | 網頁驗收：排行榜/曲線/日誌/檔案室全部吃真實 DB 資料 |

**回放測試模式**（P3 驗收關鍵）：`agent_arena.py daily --as-of 2026-06-02`
用歷史資料逐日推進，幾分鐘內模擬完整月度循環，不必等真實
30 天——但正式對外成績只採計 live 循環（回放僅供工程驗證，
避免無意間的 look-ahead 汙染）。

## R6-10 成本估算（Gemini 四鑰池，task_class="agent_arena"）

- 日常：6 agents × ~4K in / ~0.5K out（flash）≈ 27K tokens/日 → 遠低於配額
- 月度：6 × ~8K in / ~2K out（pro）≈ 60K tokens/月
- 翻譯、評分卡、聊天各有 task affinity，不互相排擠

## 驗收原則

1. 每一筆交易的價格可追溯 `prices` 表真實列；理由原文永久保存
2. 簡報快照可重放任一天的完整決策情境
3. 引擎規則（資金/倉位/次數）由程式強制，不信任 LLM 自律
4. 績效數字誠實呈現，含虧損月；前端永久標示「模擬資金・非投資建議」
5. agent 間學習僅透過公開信與排名，策略卡互不可見
