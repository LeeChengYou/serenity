# 模擬資金池(Fund Pools)需求規格 — 草案 v0.2

> 狀態:草案,待使用者確認後派工。
> 2026-07-12 由需求討論產出。已定案:
> ① 成交模式兩種都要(下單時可選)② 支援多個資金池 ③ 進競技場排行榜與 AI agent 同場比較
> ④ 交易成本比照 agent(只有滑價,無手續費,保持可比)⑤ 初始資金可自訂,UI 預設 $3,000
> ⑥ 諮詢採「AI 公司會診」:多 agent 討論 → 綜合回報,且具記憶功能。

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

### R6 AI 公司會診(下單前的多 agent 討論 + 綜合回報 + 記憶)

概念:把 9 個 AI 經理人當成一間「AI 公司」。使用者提出議題,多個 agent 各自表態,
再由一個「主席(synthesizer)」角色彙整成綜合報告。全程落庫、可回溯、有記憶。

- R6-1 **議題**:兩種入口 —— (a) 下單面板「先問公司」:議題 = 擬下的單 + 該池持倉摘要;
  (b) 獨立提問:開放式問題(如「NVDA 現在該加碼嗎?」),不綁定下單。
- R6-2 **與會者**:預設 = 該 symbol 所屬領域(semis / robotics / ai_cloud)的 3 個 agent;
  使用者可手動勾選,上限 5 人(控制 Gemini 配額)。
- R6-3 **兩階段流程**(LLM 呼叫數 = N+1,預設 4 次):
  1. 意見輪:每位與會 agent 各 1 次呼叫。輸入 = 議題 + 該 agent 當日 briefing
     (as_of 以前資料,零 look-ahead)+ 該 agent 自己的 `agent_memory`(重用現有表)
     + 本池同 symbol 的歷史會診摘要與事後結果(見 R6-5)。
     輸出 = 立場(支持/反對/中立)+ 信心(0-1)+ 理由。
  2. 綜合輪:主席 1 次呼叫。輸入 = 全部意見。輸出綜合報告:共識與分歧點、
     多空論點對照、建議行動、主要風險。主席是獨立 prompt 角色,不屬於 9 個 agent。
  - 「agent 互看意見的第二輪交叉討論」列為 V2,V1 不做(配額考量)。
- R6-4 **落庫**:`pool_consults`(議題、綜合報告、symbol)+ `pool_consult_opinions`
  (每位 agent 的立場/信心/理由)。若之後真的下單,回填 trade_id 與 followed(是否照做)。
- R6-5 **記憶功能**:記憶 = 歷史會診紀錄 + 事後結果回填,不另建記憶表。
  - daily 流程對已滿 7 個交易日的會診自動回填 `outcome_7d`(該 symbol 其後 7 交易日
    報酬,用 `prices` 真實價,缺價則 NULL —— 零捏造)。
  - 下次會診同一 symbol 時,把最近 K 次(預設 3)該 symbol 會診的「綜合報告摘要 +
    當時建議 + outcome_7d + 使用者是否照做」注入意見輪與綜合輪 prompt,
    讓 AI 公司「記得上次說過什麼、對了沒」。
  - agent 個人層記憶直接重用現有 `agent_memory`,不重複造輪子。
- R6-6 **統計**:「聽公司建議 vs 不聽」的事後表現對照 —— 本功能的研究價值所在。
- R6-7 **降級**:走現有 backend 抽象(GeminiBackend / StubBackend);測試一律 StubBackend。
  某 agent 429/失敗 → 標注「缺席」,只要 ≥1 份意見即可出綜合報告;
  全數失敗 → 提示稍後再試。會診永遠不阻擋下單(諮詢是輔助,不是關卡)。

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

-- 會診主檔(一次 AI 公司會診一列)
create table pool_consults (
    id          integer primary key autoincrement,
    pool_id     text not null,
    trade_id    integer,             -- 之後若真的下單,回填 agent_trades.id
    symbol      text,                -- 議題主要標的(記憶檢索用;開放式提問可 NULL)
    as_of       text not null,
    question    text not null,       -- 議題:擬下的單 + 持倉摘要,或開放式問題
    summary     text,                -- 主席綜合報告(全部意見失敗時 NULL)
    followed    integer,             -- null=未下單 / 1=照建議做 / 0=沒照做
    outcome_7d  real,                -- 事後 7 交易日報酬(daily 回填;缺價 NULL)
    created_at  text not null
);

-- 會診個別意見(每位與會 agent 一列)
create table pool_consult_opinions (
    id          integer primary key autoincrement,
    consult_id  integer not null,
    agent_id    text not null,
    stance      text not null,       -- support | oppose | neutral | absent(429/失敗)
    confidence  real,
    opinion     text not null
);
```

持倉、交易、NAV 重用 `agent_positions` / `agent_trades` / `agent_nav_daily`
(agent_id = pool id)。`agent_trades` 需加欄位 `fill_mode text default 't1_open'`。

## 6. API 端點(server.py)

- `POST /api/pools` 建池;`GET /api/pools` 列表(含摘要指標)
- `POST /api/pools/{id}/orders` 下單(含 fill_mode、reason);`GET .../trades`、`.../positions`、`.../nav`
- `POST /api/pools/{id}/archive`
- `POST /api/pools/{id}/consult` 發起 AI 公司會診(與會者、議題);`GET /api/pools/{id}/consults` 歷史會診
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
7. 會診(StubBackend):意見輪 N 份意見 + 主席綜合報告皆落庫;trade_id / followed 回填正確。
8. 會診記憶:同 symbol 第二次會診時,prompt 內含上次會診摘要與 outcome_7d(以 Stub 檢查輸入)。
9. outcome_7d 回填:跑 daily 滿 7 交易日後自動填入,數值 = prices 真實價計算;缺價 NULL。
10. 降級:模擬 1 個 agent 失敗 → stance='absent' 且綜合報告仍產出;全失敗 → summary NULL、API 回明確錯誤,且不影響下單流程。
11. 既有競技場測試(scratch/test_arena_final.py)全數仍通過 —— 不破壞現有功能。

## 9. 已定案事項(原未定事項)

- 交易成本:比照 agent —— 只有滑價(SLIPPAGE_BPS)、無手續費,雙方規則一致即可比。
- 初始資金:使用者可自訂,UI 預設 $3,000(與 agent 相同,便於同榜對照)。
- 諮詢形式:AI 公司會診(多 agent 意見輪 + 主席綜合輪),預設同領域 3 人、上限 5 人;
  第二輪交叉討論列 V2。

## 10. 介面契約(由監督者定死;驗收測試與實作都以此為準,矛盾時以驗收測試為準)

### 10.1 `scripts/fund_pool.py`(新檔,引擎層,風格比照 agent_arena.py)

```
constants:
  DEFAULT_INITIAL_CASH = 3000.0        # UI 預設,建池可自訂
  CONSULT_MAX_PARTICIPANTS = 5
  CONSULT_MEMORY_K = 3                 # 記憶注入:同 symbol 最近 K 次會診
  OUTCOME_TRADING_DAYS = 7

migrate(con) -> None
  冪等。建 pools / pool_consults / pool_consult_opinions(schema 見 §5);
  agent_trades 加欄 fill_mode text not null default 't1_open'(已存在則略過)。

create_pool(con, name, initial_cash, created_at=None) -> str   # 回傳 pool_id
  pool_id = 'pool-<slug(name)>'。seed:agents 列(id=pool_id, domain='human',
  style_seed='human', backend='human', status='active', hwm=initial_cash)、
  agent_state(cash=nav=initial_cash)、pools 列。同名池已存在 → raise ValueError。

place_order(con, pool_id, side, symbol, *, usd=None, qty=None,
            reason, fill_mode='t1_open', as_of) -> dict
  回傳 {"status": "pending"|"filled"|"rejected", "trade_id": int|None,
        "rejected_reason": str|None, "fill_price": float|None}
  慣例:BUY 用 usd、SELL 用 qty(與 agent 相同)。
  驗證(依序,先中先拒):reason 為空、池不存在或 archived、symbol 不在 prices、
  BUY usd > 現金、SELL qty > 持有。
  t1_open:insert agent_trades(status='pending', decided_date=as_of,
    fill_mode='t1_open'),之後由 run_daily 的 _fill_pending_orders 以
    當日開盤 ± SLIPPAGE_BPS 撮合(現有引擎,不改)。
  latest_close:取 prices 中該 symbol date <= as_of 的最新 close,
    fill_px = close × (1±slip),立即更新 positions/cash,
    status='filled', exec_date=該價格的 date, fill_mode='latest_close'。

archive_pool(con, pool_id) -> None      # pools.status='archived' 且 agents.status='retired'
list_pools(con) -> list[dict]

class ConsultBackend(ABC):
    opine(agent_id, prompt) -> dict     # {"stance":"support|oppose|neutral","confidence":float,"opinion":str}
    synthesize(prompt) -> str
class StubConsultBackend(ConsultBackend):
    __init__(opine_map=None, synthesize_text="stub summary")
    # 必須記錄收到的輸入供測試檢查記憶注入:
    # .opine_prompts: list[tuple[agent_id, prompt]]、.synthesize_prompts: list[str]
class GeminiConsultBackend(ConsultBackend)   # 用 serenity.gemini.call_gemini

run_consult(con, pool_id, question, symbol, participants, as_of, backend) -> int  # consult_id
  participants:1..5 個 agent_id。意見輪:逐一 backend.opine(),prompt 必含
  question + 該 agent 當日 briefing(agent_arena.build_briefing,零 look-ahead)
  + 該 agent 的 agent_memory + 該池同 symbol 最近 CONSULT_MEMORY_K 次會診
  (summary + outcome_7d + followed)。opine 例外/429 → 該 agent stance='absent'。
  綜合輪:≥1 份非 absent 意見 → backend.synthesize() 存 summary;全 absent → summary NULL。

backfill_outcomes(con, as_of) -> None
  冪等。對 outcome_7d IS NULL 且 symbol 非 NULL 的會診:若 prices 中該 symbol
  在會診日之後已有 ≥7 個交易日,outcome_7d = 第 7 交易日 close / 會診日(<=as_of)
  最近 close - 1;缺價維持 NULL(零捏造)。

CLI:python scripts/fund_pool.py migrate | daily(daily = backfill_outcomes(最新交易日))
```

### 10.2 `scripts/agent_arena.py` 修改(最小侵入)

- `run_daily`:agents 查詢加選 backend 欄;`backend='human'` 的列**跳過**
  簡報/decide/下單/memory(步驟 3–7),但仍執行冪等檢查與 `_record_nav`。
  (`_fill_pending_orders` 為全域撮合,human 的 t1_open 單自動涵蓋,不用改。)
- `run_monthly`:排名/淘汰/relaunch/reflect 全部排除 `backend='human'`。
- 其餘不動;`migrate` 不動(新表由 fund_pool.migrate 負責)。

### 10.3 `serenity/services/pool_views.py`(新檔)+ arena_views 修改

- `pool_list_payload(con)` → `{"pools":[{pool_id,name,initial_cash,status,nav,cash,
  total_return_pct,mdd,pending_orders,created_at}]}`
- `pool_detail_payload(con, pool_id)` → 持倉(qty/avg_cost/last_close/unrealized_pnl/
  weight_pct)、nav 序列、trades(含 fill_mode/reason/status)
- `pool_consults_payload(con, pool_id)` → 會診列表(含 opinions 與 summary)
- `arena_leaderboard_payload`:列加 `kind` 欄('ai'|'human'),human 池列入;
  既有欄位與既有測試不得壞。

### 10.4 API 路由(serenity/api/handler.py)

- `GET  /api/pools`、`POST /api/pools`(body: name, initial_cash)
- `GET  /api/pools/{id}`(detail)、`POST /api/pools/{id}/archive`
- `POST /api/pools/{id}/orders`(body: side, symbol, usd|qty, reason, fill_mode,
  as_of 可省 = prices 最新日)→ place_order 結果原樣回傳
- `POST /api/pools/{id}/consult`(body: question, symbol, participants 可省 =
  該 symbol 領域 3 agent)、`GET /api/pools/{id}/consults`

### 10.5 Dashboard(dashboard/ 新「資金池」分頁,zh-TW)

池列表卡 + 建池表單、下單面板(symbol 搜尋、方向、金額/股數、成交模式、reason 必填、
「先問 AI 公司」)、持倉表、交易紀錄表、NAV 曲線(可疊加 agent)、會診紀錄
(個別意見 + 綜合報告 + 事後 outcome)。

---
Changelog:
- 2026-07-12 v0.3 補 §10 介面契約(派工用,監督者定稿)。
- 2026-07-12 v0.2 定案手續費/初始資金;諮詢升級為 AI 公司會診(多 agent + 主席綜合 + 記憶/outcome 回填)。
- 2026-07-12 v0.1 初稿(需求討論 session)。
