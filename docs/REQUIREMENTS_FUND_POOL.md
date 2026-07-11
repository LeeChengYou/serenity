# 模擬資金池(Fund Pools)需求規格 — 草案 v0.1

> 狀態:草案,待使用者確認後派工。
> 2026-07-12 由需求討論產出。已定案的三個方向:
> ① 成交模式兩種都要(下單時可選)② 支援多個資金池 ③ 進競技場排行榜與 AI agent 同場比較。

## 1. 目標

讓使用者本人用虛擬資金做 paper trading:建立多個資金池、對 89 檔追蹤股票下單、
追蹤持倉與淨值,並與競技場 9 個 AI agent 同榜比較。下單前可向 agent 諮詢意見,
形成「human + AI 協作」的操作與檢討迴圈。

**不做**:真實券商、真單、保證金/放空、盤中即時報價(資料只有日線)。

## 2. 核心概念

- **資金池(pool)**:一組獨立的虛擬資金 + 持倉 + 交易紀錄 + 每日 NAV。
  使用者可建多個池做策略對照(例如「跟著 momentum」vs「自己的價值策略」)。
- 池在架構上是一個 `backend='human'` 的特殊 agent(見 §7 設計決策),
  以最大化重用競技場現有引擎:撮合、NAV 記帳、排行榜。

## 3. 功能需求

### R1 資金池管理
- R1-1 建池:名稱(唯一)、初始虛擬資金(使用者自訂,USD)、建立日。
- R1-2 多池並存,無數量上限(UI 合理即可)。
- R1-3 封存(archive):停止交易但保留全部歷史與 NAV 曲線。**不提供刪除**(誠實績效,不能抹掉難看的紀錄)。
- R1-4 不允許事後增資/出金 —— 初始資金固定,績效才可比。要更多資金就開新池。

### R2 下單與成交
- R2-1 支援 BUY / SELL 市價單;BUY 以金額(USD)下單、SELL 以股數下單(與 agent 相同慣例,支援小數股)。
- R2-2 **成交模式二選一,下單時指定,記錄於 `fill_mode` 欄位**:
  - `t1_open`(預設):與 agent 完全同規則 —— 決策日之後第一個有價交易日的
    **開盤價 ± SLIPPAGE_BPS 滑價**成交,重用 `_fill_pending_orders`。零 look-ahead。
  - `latest_close`:下單當下以 DB 最新收盤價 ± 滑價立即成交。體感即時,
    但使用者已見過該價,屬輕微 look-ahead → 交易紀錄與績效統計必須標注。
- R2-3 驗證規則與 agent 對齊(重用/比照 `_validate_and_record_actions`):
  - 現金不足 → 拒單(`rejected_reason`)
  - 賣出股數 > 持有 → 拒單
  - symbol 不在 `prices` 表 → 拒單
- R2-4 每筆單**強制填寫 reason**(操作理由,供日後檢討,與 agent trades 對齊)。
- R2-5 拒單、pending、filled 狀態全程留痕,不可刪改。

### R3 持倉與淨值
- R3-1 每池獨立:cash、positions(qty、avg_cost)、每日 NAV。
- R3-2 NAV 記帳重用 `_record_nav` 同一邏輯,掛進 `agent_arena.py daily` 流程
  (human 池只跑撮合與記帳,**不跑** briefing/decide/reflect)。
- R3-3 顯示:未實現損益(現價 vs 均價)、已實現損益、個股佔比、總報酬、MDD。
- R3-4 缺價日照競技場現規則處理(單留 pending、NAV 用最近可得收盤價),不捏造價格。

### R4 儀表板 UI(dashboard/ 新頁「資金池」)
- R4-1 池列表 + 總覽卡:NAV、現金、日/總報酬、MDD、未成交單數。
- R4-2 下單面板:symbol 選擇(watchlist 優先 + 全 89 檔搜尋)、方向、金額/股數、
  成交模式、reason 必填。送出前顯示預估(最新收盤價僅供參考,T+1 單註明「實際以次日開盤成交」)。
- R4-3 持倉表、交易紀錄表(含狀態、fill_mode、reason、諮詢紀錄連結)。
- R4-4 NAV 曲線;可疊加任選 agent 的 NAV 曲線做對照。

### R5 與競技場整合
- R5-1 human 池出現在競技場排行榜,明確標記「HUMAN」。
- R5-2 human 池**不參與** agent 的淘汰/relaunch/monthly reflect 機制。
- R5-3 使用者可查看當日各 agent briefing 作決策參考(已有資料,唯讀)。

### R6 Agent 諮詢(下單前問 AI 經理人)
- R6-1 下單面板可選 1 個 agent 諮詢:送出「擬下的單 + 該池目前持倉 + 該 agent
  當日 briefing(as_of 當日以前資料,零 look-ahead)」,agent 回覆支持/反對 + 理由。
- R6-2 諮詢紀錄落庫(pool_consults):問了誰、agent 說什麼、最終是否照做。
  之後可統計「聽 agent vs 不聽」的勝率 —— 這是本功能的研究價值所在。
- R6-3 走現有 backend 抽象(GeminiBackend / StubBackend);測試一律 StubBackend。
  諮詢是 on-demand 低頻呼叫,注意 Gemini 429 時要優雅降級(顯示稍後再試,不阻擋下單)。

## 4. 鐵律對照(全部適用)

- 零捏造:所有成交價、NAV 均來自 `prices` 真實列;缺價就 pending / 沿用舊價並標注。
- 零 look-ahead:`t1_open` 完全乾淨;`latest_close` 為已知的例外,以 fill_mode 標注、統計註記。
- 只做 paper trading。

## 5. 資料表(草案)

```sql
-- 池主檔(human 池同時在 agents 表有一列 backend='human',見 §7)
create table pools (
    agent_id     text primary key,   -- 'pool-<slug>',同時是 agents.id
    display_name text not null,
    initial_cash real not null,
    status       text not null default 'active',  -- active | archived
    created_at   text not null
);

-- 諮詢紀錄
create table pool_consults (
    id          integer primary key autoincrement,
    pool_id     text not null,
    trade_id    integer,             -- 之後若真的下單,回填 agent_trades.id
    agent_id    text not null,       -- 被諮詢的 agent
    as_of       text not null,
    question    text not null,       -- 擬下的單 + 持倉摘要
    answer      text not null,       -- agent 回覆
    followed    integer              -- null=未下單 / 1=照做 / 0=沒照做
);
```

持倉、交易、NAV 重用 `agent_positions` / `agent_trades` / `agent_nav_daily`
(agent_id = pool id)。`agent_trades` 需加欄位 `fill_mode text default 't1_open'`。

## 6. API 端點(server.py)

- `POST /api/pools` 建池;`GET /api/pools` 列表(含摘要指標)
- `POST /api/pools/{id}/orders` 下單(含 fill_mode、reason);`GET .../trades`、`.../positions`、`.../nav`
- `POST /api/pools/{id}/archive`
- `POST /api/pools/{id}/consult` agent 諮詢
- 排行榜端點加入 human 池(標記 kind='human')

## 7. 設計決策:human 池 = 特殊 agent

採「B 案」:在 `agents` 表 seed 一列 `backend='human'`、`domain='human'`,
使 `_fill_pending_orders`、`_record_nav`、NAV/排行榜查詢**零改動**直接涵蓋。
代價與對策:
- daily 流程需跳過 human 池的 briefing/decide;monthly 淘汰/reflect 需排除 `backend='human'`。
- `style_seed` 等欄位對 human 無意義 → 填固定佔位值。
- 風險:所有動到「全體 agents」的既有查詢都要檢查是否該排除 human(派工時列入驗收)。

## 8. 驗收測試(派工前先寫,比照 scratch/test_arena_final.py)

1. 建池 → T+1 買單 → 跑 daily → 成交價 = 次日開盤 × (1+slip),現金/持倉/NAV 正確。
2. `latest_close` 單:立即成交、fill_mode 標注正確、成交價 = 最新收盤 × (1±slip)。
3. 現金不足 / 賣超 / 無此 symbol → 三種拒單各自 rejected_reason 正確。
4. reason 空白 → API 拒收。
5. 排行榜含 human 池且標記正確;monthly 淘汰邏輯不碰 human 池。
6. 封存池拒絕新單,歷史查詢正常。
7. 諮詢(StubBackend):落庫、trade_id 回填、429 降級不阻擋下單。
8. 既有競技場測試(scratch/test_arena_final.py)全數仍通過 —— 不破壞現有功能。

## 9. 未定事項(實作前確認)

- 手續費:目前 agent 只有滑價無手續費;human 池是否比照?(建議比照,保持可比)
- 預設初始資金建議值(agent 是 $3,000;human 池自訂,UI 預設值待定)
- 諮詢一次問多個 agent?(建議 V1 先單一 agent,降低 Gemini 配額壓力)

---
Changelog:
- 2026-07-12 v0.1 初稿(需求討論 session)。
