# AI 分析升級三部曲需求規格 — (a) 聊天市場總覽 / (b) 本地 LLM / (c) 台股支援

> 狀態:(a) 詳細規格,本輪實作;(b)(c) 路線圖級,待 (a) 完成後細化。
> 2026-07-12 需求討論定案:三個都做,順序 a → b → c。

## 背景(現況)

儀表板聊天室 = `serenity/services/chat.py` 的 `handle_chat_api`:
Gemini(聊天視窗可選模型)+ `skills/serenity-skill/SKILL.md` 研究準則 +
逐股 DB 檢索(訊息中的代號與主題關鍵字 → 相關貼文/價格/提及數,auto-associate
上限 5 檔)+ `user_memories` 長期記憶。**缺市場層 context**:問「大盤如何」「哪個
類股強」時模型拿不到任何全市場資料,只能空談。

---

## (a) 聊天市場總覽 context 強化【本輪實作】

### a-R1 市場總覽快照注入
- 新函式 `build_market_overview(con, extended=False) -> str`(chat.py 模組層,可獨立測試)。
- **每次對話都注入精簡版**(所有數字來自 DB,零捏造;缺資料標「—」或整行省略):
  - 資料日:prices 最大 date(明示「日線資料」)
  - 市場狀態:重用 `regime_payload(con)`(regime + SPY/SOXX 相對 EMA200 + 站上
    EMA50 比例;unknown 時如實說明)
  - 當日漲幅前 5 / 跌幅前 5:symbol、漲跌%、收盤價
  - 觀察清單快照:每檔 symbol + 當日漲跌%(清單空 → 「(空)」)
  - 近 7 日 X 討論熱度前 5:symbol + 提及次數
- 行情數字**重用 `pool_views.market_board_payload(con)`**(單一資料來源,
  不重寫查詢),7 日熱度另以 mentions 表現查。

### a-R2 市場意圖偵測 → 進階版快照
- `_detect_market_intent(msg) -> bool`:訊息含市場級關鍵字(市場、大盤、總覽、
  類股、板塊、前景、趨勢、輪動、環境、宏觀、整體、market、outlook、sector、
  macro、regime…)即 True。
- True 時注入**進階版**:漲跌幅各前 10 + 5 日%欄、成交量前 5、觀察清單加 5 日%。

### a-R3 System prompt 強化
- 註明快照資料日與「日線非即時」;明示模型可做多股比較與市場層分析,
  但一切結論必須錨定注入的快照與檢索資料;維持既有反幻覺條款不動。

### a-R4 相容性
- 既有逐股檢索、主題 RAG、記憶、serenity-skill 注入**行為不變**。
- `/api/chat` 介面(payload/回傳)不變;前端零改動。

### a-驗收(scratch/test_chat_market.py,不打真 Gemini,只測 builder)
1. `build_market_overview` 輸出含 prices 最大 date。
2. 漲幅第一名 symbol 與 SQL 現算一致;其漲跌%字串與 SQL 現算一致(2 位小數)。
3. 觀察清單有值時,清單 symbol 出現在快照;清空時顯示「(空)」。
4. `_detect_market_intent`:「大盤現在怎麼看」「哪個類股最強」→ True;
   「分析 NVDA」「TSLA 財報」→ False。
5. extended=True 內容嚴格多於 compact(前 10 vs 前 5;含「5日%」欄位標記)。
6. 快照字串不含 "None";benchmarks 缺資料時 regime 行如實顯示 unknown 說明。
7. `python -m py_compile serenity/services/chat.py` 通過;import server 不壞;
   既有 test_fund_pool.py 147/147、test_arena_final.py 70/70 不受影響。

---

## (b) 本地 LLM backend(Ollama / OpenAI-compatible)【b-1 詳細規格,2026-07-12 定稿】

硬體已確認:RTX 4070 Ti SUPER(16GB VRAM)。

### b-R1 引擎與模型
- 引擎:**Ollama**(底層 llama.cpp;拿到 prefix cache、keep_alive 常駐等能力)。
  安裝與模型下載由**使用者本人執行**(`ollama pull qwen3:14b`)。
- 預設模型:`qwen3:14b`(Q4 約 9–10GB,16GB 舒適;中文金融理解優於 Llama)。
  模型名可在設定改;Llama-3.1-8B 等留給純英文批次任務自選。
- 呼叫走 **OpenAI-compatible** `POST {base}/v1/chat/completions`
  (未來可無痛換 LM Studio / vLLM);base 預設 `http://127.0.0.1:11434`。

### b-R2 `serenity/llm_local.py`(新)
```
call_local_llm(messages, system=None, temperature=0.3,
               model=None, base_url=None, timeout=120) -> str
  - model/base_url 缺省時讀 settings(local_llm_model / local_llm_base_url)
  - keep_alive 參數設 -1(模型常駐 VRAM)
  - 連線失敗/逾時 → raise LocalLLMUnavailable(zh-TW 訊息:
    「本地模型未啟動:請先啟動 Ollama(ollama serve)並確認已 pull {model}」)
  - 回應非預期結構 → raise(顯式,不靜默補值)
is_local_llm_up(base_url=None) -> bool   # GET {base}/api/tags,1 秒逾時
```
- **Prefix cache 友善**:呼叫端組 prompt 時,不變內容(skill 準則)排最前,
  變動內容(市場快照、個股檢索)排後——寫進 chat 整合的實作註記。

### b-R3 聊天室整合
- 聊天視窗模型下拉加「本地 Ollama(qwen3:14b)」選項(value=`local`)。
- `handle_chat_api`:model==`local` → 走 `call_local_llm`(不需 Gemini key);
  LocalLLMUnavailable → 回友善錯誤訊息(200 + error 欄位,不 500)。
- 設定頁(既有 settings 機制)加 `local_llm_base_url`、`local_llm_model`。

### b-R4 會診整合
- `scripts/fund_pool.py` 加 `LocalConsultBackend(ConsultBackend)`:
  opine/synthesize 走 call_local_llm,JSON 解析失敗顯式 raise(由 run_consult
  標 absent,與 Gemini 路徑同語義)。
- `/api/pools/{id}/consult` body 加選填 `backend`(`gemini` 預設|`local`)。

### b-R5 範圍外(b-2 再做)
- arena agent 日常決策切本地、批次情緒/翻譯切本地、每任務類別的 backend 指派表。

### b-驗收(scratch/test_local_llm.py;用本機假 HTTP server 模擬 Ollama,不需真裝)
1. call_local_llm 對假 server 正常解析回覆文字。
2. 假 server 回畸形 JSON / 缺欄位 → 顯式 raise,不回假值。
3. 連不上(未啟動)→ LocalLLMUnavailable,訊息含「本地模型未啟動」。
4. is_local_llm_up:有假 server True / 無 False。
5. handle_chat_api(model='local', 假 server)成功回覆;假 server 關閉 →
   回傳含 error 的 dict 而非例外。
6. LocalConsultBackend:StubHTTP 下 run_consult 全流程落庫;既有
   test_fund_pool.py 147/147、test_arena_final.py 70/70 不受影響。
7. py_compile 全部改動檔;真機冒煙(使用者裝好 Ollama 後):聊天選「本地」
   問一題,回覆引用快照數據。

## (c) 台股支援 Phase 1【詳細規格,2026-07-12 定稿】

現況盤點(已查證):價格來源是 Yahoo v8 chart API(`ingest.yahoo_chart`),
**原生支援 `.TW`/`.TWO`**;watchlist 內的代號本來就會進 `symbol_list` →
`fetch_prices` 抓價。Phase 1 = 打通入口 + 地區標示 + 誠實邊界。

### c-R1 入口:watchlist 收台股代號
- `serenity/services/watchlist.py` 的 `_SYM_RE` 改為 `^[A-Za-z0-9.\-]{1,12}$`
  (現況不允許數字,2330.TW 會被擋)。
- `scripts/ingest.py` **新增** `fetch_prices_for_symbol(con, symbol, days_back=420)`:
  單一代號版抓價(重用 `yahoo_chart` 與既有 upsert 邏輯);watchlist.py 的
  背景補抓已在呼叫此函式名(現在 AttributeError 靜默略過 → 修好後真正生效)。
- 行情看盤板加「＋新增代號」輸入框:POST /api/watchlist {add} →
  提示「已加入,價格抓取中,約 1 分鐘後重新整理」。

### c-R2 地區標示與篩選
- `region(symbol)` 規則:`.TW`/`.TWO` 結尾 → `tw`,其餘 → `us`
  (共用工具函式,放 serenity/services/pool_views.py 或獨立小模組)。
- `market_board_payload` rows 加 `region` 欄。
- 行情板加地區篩選鈕:全部 / 🇺🇸 美股 / 🇹🇼 台股(樣式比照「只看觀察清單」)。
- 台股列價格顯示 `NT$` 前綴(美股維持 `$`);個股詳情標題同理。

### c-R3 誠實邊界(Phase 1 明確不做的,要擋好)
- **資金池下單**:`place_order` 對 region='tw' 的 symbol 拒單,
  rejected_reason=「台股暫不支援下單(TWD 幣別,Phase 3)」——USD 池混 TWD
  價格會汙染 NAV,必須硬擋。會診(read-only)不擋。
- 深度研究:technical 對台股自然可用(有價就能算);events/valuation 會缺 →
  既有「—/尚無資料」語義已誠實,不需特殊處理。
- 聊天代號偵測:`chat.py` 的 db_symbols 從 mentions 擴為 mentions ∪ prices
  (台股無 X 提及,但有價格就該能被問)。

### c-驗收(scratch/test_tw_phase1.py;tempfile DB 副本;不打真 Yahoo——
fetch_prices_for_symbol 的網路呼叫用 monkeypatch 假 yahoo_chart)
1. `_SYM_RE`:2330.TW / 6488.TWO 過,`$;DROP` / 超長不過;既有美股代號仍過。
2. `region()`:2330.TW→tw、6488.TWO→tw、NVDA→us、BRK.B→us(`.B` 不是台股)。
3. `fetch_prices_for_symbol`(假 yahoo_chart 回 3 根日線)→ prices 表 3 列,
   重跑冪等(upsert 不重複)。
4. `market_board_payload`:在 DB 副本插入台股假價格列 → rows 含 region='tw'
   該列,美股列 region='us'。
5. `place_order` BUY 2330.TW → rejected,reason 含「台股」;美股單不受影響。
6. chat 代號偵測:prices 有 2330.TW(mentions 無)時,`handle_chat_api` 的
   context 建構能抓到該代號(測 db_symbols 來源擴充;不打 LLM)。
7. 既有五套測試(deep_dive 58、fund_pool 147、arena 70、chat_market 18、
   local_llm 16)不受影響;py_compile;node --check。
8. 真機冒煙(merge 後由監督者執行):加 2330.TW 進 watchlist → 實抓價格 →
   行情板出現台股列(NT$、region 篩選有效)→ 深度研究 technical 有數字。

Phase 2(路線圖):台股新聞/PTT 討論源 + 中文情緒;Phase 3:資金池 TWD 池或匯率換算。

---

## (d) 個股深度研究報告(deep dive)【2026-07-12 定稿,實作中】

原則:**數字由 python 確定性計算,LLM 只做綜合解讀**(教訓:14B 模型會把
1.47 億唸成 14.7 億)。所有價位可追溯到計算來源;報告永遠帶「模擬用途,
非投資建議」;樣本不足如實標注(本 DB 每檔新聞事件日僅約 5-6 天)。

### d-R1 `serenity/services/deep_dive.py::deep_dive_payload(con, symbol, as_of=None) -> dict`

全部計算只用 `date <= as_of` 的資料(零 look-ahead);as_of 缺省 = 該 symbol
prices 最大 date;無價格 → `{"error": ...}`。欄位與**精確算法**(驗收測試以此為準):

- `technical`(最近 250 交易日;不足用可得的並回報 `n_days`):
  - `rsi14`/`ema20`/`ema50`/`ema200`:**重用** `agent_arena._calc_rsi14`/`_calc_ema`(不足 → None)
  - `atr14`:Wilder ATR。TR = max(high−low, |high−prev_close|, |low−prev_close|);
    前 14 個有效 TR 簡單平均起始,之後 `(prev×13+TR)/14` 平滑;high/low 為 NULL 的日子跳過;有效 TR < 14 → None
  - `ann_vol_pct`:最近 120 個日報酬(close/close−1)樣本標準差 × √252 × 100;不足 30 筆 → None
  - `hi_60d`/`lo_60d`:最近 60 交易日 close 極值
  - `support_levels`:最近 60 交易日 swing low(close 嚴格低於前後各 2 日)最近 3 個(由近到遠);`resistance_levels` 同理 swing high
  - `max_drawdown_1y_pct`(250 日 close 最大回撤,正值)、`chg_20d_pct`
- `events`(news_sentiment,`date(published_at) <= as_of`):
  日聚合 bull/bear 筆數;**正面事件日 = bull≥2 且 bull>bear;負面 = bear>bull**。
  事件基準 = 事件日或其後第一個交易日 close;`d1/d5/d10` = 第 1/5/10 交易日
  close/基準 −1;僅統計 forward 窗完整(≤ as_of)的事件。
  輸出 positive/negative 各 `{n, d1_mean_pct, d1_win_rate, d5_…, d10_…}`
  (n=0 → 其餘 None)+ `insufficient`(正負合計 n<10 → true)。
- `valuation`:fundamentals(pe/forward_pe/revenue_growth_yoy/next_earnings_date)
  + analyst_estimates(target_low/median/mean/high、n_analysts、recommendation_key)
  + `upside_to_median_pct` = target_median/close −1;任何缺值 → None,不捏造。
- `reference_levels`(確定性參考位,非預測;每個附 `basis` 說明來源):
  - `stop_loss` = close − 2×atr14
  - `entry_zone` = [最近支撐位, 支撐位 + 0.5×atr14](支撐與 atr 缺一 → None)
  - `exit_zone` = 最近壓力位與 target_median 可得者構成 [min, max];全缺 → None

### d-R2 `deep_dive_report(con, symbol, backend='local', as_of=None) -> dict`

numeric payload → LLM 綜合(`local` 走 call_local_llm / `gemini` 走 call_gemini)。
prompt 強制:所有數字**預先由 python 格式化**成文字(2 位小數、億/萬單位);
每個價位必須引用 payload 欄位;樣本不足如實說;結尾固定免責聲明。
落庫 `deep_dive_reports(id, symbol, as_of, close, entry_lo, entry_hi, exit_lo,
exit_hi, stop_loss, narrative, backend, created_at, outcome_7d)`(outcome
回填屬 d-2,本輪只建欄)。LLM 失敗 → 回 numeric + error 欄(不 500)。

### d-R3 API

`GET /api/deepdive/{symbol}`(numeric)、`POST /api/deepdive/{symbol}/report`
(body: backend,預設 local;非法 → 400)、`GET /api/deepdive/{symbol}/reports`。

### d-R4 UI

個股詳情加「深度研究」tab:進 tab 即載入 numeric 四區塊(技術結構/事件研究
/估值錨/參考位,含 basis 與樣本數);「產生 AI 解讀」(下拉 本地/Gemini)→
narrative + 歷史報告列表。快取升版 `20260712-fundpool5` / `serenity-v6-deepdive`。

### d-R5 會診整合

`run_consult` 意見輪 prompt 附該 symbol 的 technical + reference_levels
精簡文字塊(由 deep_dive_payload 轉,python 預格式化)。

### d-驗收(scratch/test_deep_dive.py;tempfile DB 副本;LLM 全用假 server/Stub)

1. technical 每個數字與測試內**獨立重算**一致(ATR/年化波動/swing/回撤/20日%,
   誤差 1e-6 相對);rsi/ema 與 agent_arena 函式輸出一致。
2. events:以 SQL 重算事件日集合一致;抽 1 個事件驗 d5 報酬;forward 窗不完整
   的事件被排除;正負合計 <10 → insufficient=true。
3. valuation:與 fundamentals/analyst_estimates 原始列一致;缺值 → None。
4. reference_levels:stop_loss = close−2×ATR;entry/exit zone 組成正確;
   atr 缺 → 相關位 None;basis 非空。
5. as_of 參數:指定過去日期 → 所有輸出只用該日(含)以前資料(抽 hi_60d 驗證)。
6. deep_dive_report:假 LLM server 下 narrative 落庫、報告列完整;LLM 失敗 →
   回 numeric+error 不拋例外;backend 非法 → API 400。
7. 會診 prompt(StubConsultBackend.opine_prompts)含 technical 區塊標記。
8. 既有全部測試(fund_pool 147、arena 70、chat_market 18、local_llm 16)不受影響;
   py_compile;node --check。

---
Changelog:
- 2026-07-12 初版;(a) 詳細規格,(b)(c) 路線圖。
