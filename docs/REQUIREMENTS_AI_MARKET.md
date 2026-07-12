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

## (b) 本地 LLM backend(Ollama / OpenAI-compatible)【路線圖】

- 目標:量大低風險呼叫(批次情緒標注、翻譯、arena agent 日常決策、會診意見輪)
  可切到本地模型,擺脫 Gemini 429;互動聊天預設留雲端。
- 建議模型:Qwen2.5-14B-Instruct(中文金融理解佳;Q4 約 10–12GB VRAM,
  8B 版 6–8GB 為低配替代)。
- 架構:新增 `serenity/llm_local.py` 提供 OpenAI-compatible `/v1/chat/completions`
  呼叫;`AgentBackend`/`ConsultBackend` 各加 Ollama 實作;設定頁加
  「backend 指派」(每種任務類別選 gemini / ollama)。
- 驗收要點:Ollama 未啟動時優雅降級(fallback 到 gemini 或明確報錯不 500);
  Stub 測試涵蓋解析畸形輸出。
- 前置確認:使用者 GPU VRAM(決定 8B/14B/32B)。

## (c) 台股支援 Phase 1【路線圖】

- 範圍:台股代號(`2330.TW`/`.TWO`)進 prices ingest、行情看盤板、觀察清單、
  個股詳情走勢圖。**不含**:訊號評分/情緒(X cashtag 無台股討論,Phase 2 接
  中文資料源)、資金池台股下單(幣別問題,Phase 3 決定獨立 TWD 池或匯率換算)。
- 改動面:ingest 的 symbol 宇宙加 region 欄;yfinance 抓 `.TW` 日線(注意時區
  與交易日曆);行情板 region 篩選鈕(美股/台股);詳情頁籤對台股隱藏無資料的
  訊號/新聞 tab(誠實顯示「台股尚未支援此分析」)。
- Phase 2:台股新聞/PTT 討論源 + 中文情緒;Phase 3:資金池多幣別。

---
Changelog:
- 2026-07-12 初版;(a) 詳細規格,(b)(c) 路線圖。
