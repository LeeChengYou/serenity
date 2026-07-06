# 教訓日誌（LESSONS）

> 追加式日誌。格式與蒸餾規則見 MAINTENANCE.md 第 3、4 節。新教訓加在檔尾。

## 2026-07-06 Gemini 自創欄位導致靜默掉單
- 症狀：agent 記憶寫「今天買了 ON/GFS」但 agent_trades 空無一物，無任何報錯。
- 根因：decide() prompt 沒釘死 action schema，Gemini 回了 {sym,type,qty}；驗證無 else 分支，畸形單被靜默丟棄。
- 修法：prompt 內嵌完整 JSON schema 範例＋畸形 action 一律記為拒單（含原因）。
- 去向：已寫入 JUDGMENT.md 第 4 節反例、第 5 節品質底線；PROMPTS.md 審查模板特別檢查項。

## 2026-07-06 arena 排程單永不成交
- 症狀：模擬台北早上排程執行時，pending 單永遠等不到撮合，無報錯。
- 根因：as_of 預設為日曆日「今天」，但 prices 表最新只到上一個交易日。
- 修法：as_of 未指定時取 prices 表 max(date)（agent_arena.py `_latest_trading_date`）。
- 去向：已寫入 JUDGMENT.md 第 4 節正例。

## 2026-07-06 規格檔被 OneDrive 靜默回退
- 症狀：REQUIREMENTS_V6.md R6-1 段落自行變回舊版「兩領域 6 agent」，git 無對應 commit。
- 根因：repo 位於 OneDrive 同步目錄，版本同步覆蓋了工作區檔案。
- 修法：以 git 為事實來源；文件改完立即 commit；發現矛盾先 `git diff HEAD -- <檔>` 分辨。
- 去向：已寫入 HARNESS_DIAGNOSIS.md 問題 2、CLAUDE.md 環境陷阱。

## 2026-07-06 測試全過但 live 整合暴露 3 個真 bug
- 症狀：驗收測試 70/70 綠燈，首次 live 跑就中 3 槍（上兩條＋回頭撮合缺防護）。
- 根因：StubBackend 測不到真實 LLM 輸出的畸形性與真實排程的日曆情境。
- 修法：涉外部輸入的改動，驗收測試之外必加一次真實冒煙。
- 去向：已寫入 DISPATCH.md 第 5 節第 5 條。

## 2026-07-06 前端「修好了」但使用者看不到
- 症狀：專家觀點卡/翻譯按鈕後端 API 正常、前端碼正確，使用者頁面上就是沒有（K-2）。
- 根因（疑）：瀏覽器快取舊版 index.html/app.js；尚未最終確認。
- 修法：dashboard/ 任何改動，回報時必附「請 Ctrl+F5」；驗收以使用者實際看到為準。
- 去向：已寫入 CLAUDE.md 完成的定義、JUDGMENT.md 第 2 節反例。
