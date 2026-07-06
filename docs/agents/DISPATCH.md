# 模型調度守則（DISPATCH）

> 讀者：主對話的模型（通常是 Sonnet 或 Opus）。目的：主對話保持指揮官身分，
> context 不被工人活淹沒（此環境曾一個 session compaction 兩次，見 HARNESS_DIAGNOSIS.md 問題 1）。

## 0. 本環境實際可用的東西（2026-07-06 實測，不確定時用 `/agents`、`/model` 重新確認）

- **Agent 工具**參數：`subagent_type`（`general-purpose` / `Explore` / `Plan` /
  `claude` / `claude-code-guide`）、`model`（`sonnet` / `opus` / `haiku`；`fable` 僅
  2026-07 那次 session 可用，之後別指定它——指定了會 fallback 或報錯，直接用 `opus`）、
  `isolation: "worktree"`（隔離 worktree，適合實作類）、`run_in_background`。
- **沒有 per-agent effort 參數。** effort 是全域設定（`~/.claude/settings.json` 的
  `effortLevel`，目前 `high`；或 `/effort` 指令）。派工時不必也無法指定 effort。
- 已派出的 agent 可用 SendMessage 續派（保留它的 context）；重開 Agent 呼叫則是全新 context。
- 使用者的既定工作模式就是「監督者 + subagent 實作」（見 CLAUDE.md 工作模式），
  多步驟工程委派不需要再徵求同意；**但單檔小修、一次性查詢不要派工**——
  subagent 冷啟動要重建 context，比主對話直接做更貴。

## 1. 指揮官不下場（主對話禁區）

主對話**不做**以下事情，一律委派，只收結論：

| 禁區 | 判準 | 派給誰 |
|------|------|--------|
| 大量讀取 | 單檔 >300 行、或要開 ≥3 個檔「看看」 | Explore（唯讀，回結論+檔案:行號） |
| 掃 repo | 「X 在哪裡定義/被誰呼叫/有哪些用法」 | Explore |
| 查網頁/文件 | 需要 WebSearch/WebFetch 多輪 | general-purpose (sonnet) |
| 批次改檔 | >3 檔、或同模式機械改動 | general-purpose (sonnet) + worktree |
| 新功能實作 | 預估 >100 行新碼 | general-purpose (sonnet) + worktree |
| 驗收別人的工作 | 永遠 | fresh-context agent（見第 5 節） |

主對話**可以**直接做：讀本輪就要 Edit 的目標區段（用 offset/limit）、單檔 <50 行的修改、
跑一條驗證指令、跟使用者對話。

## 2. 派工三件套（每個派工 prompt 必含，缺一不派）

1. **目標與動機**：做什麼、為什麼做（讓 agent 遇到歧義時能自己對齊方向）。
2. **驗收條件**：可機械判定的清單（測試指令+期望輸出、檔案必須存在、endpoint 回傳格式）。
3. **回報格式**：明說「回報只要：結論 + 改動檔案清單（檔案:行號）+ 驗收結果實際輸出。
   長產物寫進檔案，回報路徑即可，不要貼全文」。

另外必附：CLAUDE.md 鐵律摘要（零捏造、零 look-ahead、機密不落 log）、
相關環境陷阱（PYTHONIOENCODING、DB 路徑）、測試用 StubBackend 不打真 Gemini。
現成模板見 [PROMPTS.md](PROMPTS.md)。

## 3. 模型指派表（顯式指定 `model`，不要用預設值碰運氣）

| 任務 | model | 理由 |
|------|-------|------|
| 列檔案、找定義、簡單掃描 | `haiku` | 便宜；錯了升級成本低 |
| 一般搜尋理解、實作、重構、測試撰寫 | `sonnet` | 性價比主力 |
| 架構設計、跨模組除錯、規格審查、第二意見 | `opus` | 判斷力型任務 |
| 已被 opus 解出模式後的批次套用 | `sonnet` 或 `haiku` | 模式已定，照抄即可 |

## 4. 升降級路徑（含重試上限）

- **haiku 錯一次 → 直接升 sonnet**（不給 haiku 第二次機會，重試比升級貴）。
- **sonnet 同一子任務連錯兩次 → 升 opus**，且 prompt 必須帶完整失敗軌跡：
  兩次嘗試各自改了什麼、測試輸出原文、目前的假設。不帶軌跡的升級會重蹈覆轍。
- **opus 解出來之後**：把解法寫成模式（規則+範例），降回 sonnet/haiku 批次套用。
- **同一件事最多重試兩輪**。兩輪後還不行 = 方向錯了，不是執行差——
  停止重試，改走 JUDGMENT.md「換路訊號」流程（換方法或問使用者）。

## 5. 驗證不自驗（誰做的就不能只由誰說做好了）

1. **驗收派 fresh-context agent**：不帶實作過程的偏見，只給它驗收條件清單去核。
2. **檔案類產出**：read-back——重新 Read 檔案確認存在且內容完整（不是空檔/半截）。
3. **程式碼類產出**：跑測試或實跑並貼輸出。arena 相關 = `scratch/test_arena_final.py`
   0 failed、exit 0；server 相關 = 重啟後 curl 端點。**「測試應該會過」不是驗證。**
4. **高風險判斷**（規格取捨、資料正確性結論）：加第二意見——派另一個 opus agent
   獨立作答，或同題產多答案後評審選優。兩者結論衝突時呈給使用者裁決。
5. 歷史教訓：驗收測試 70/70 全過，live 整合仍暴露 3 個真 bug（as_of 日曆日、
   回頭撮合、Gemini 自創欄位靜默掉單）。**測試通過 ≠ 完成；涉及外部輸入
   （LLM 回應、網路 API）的改動要加一次真實冒煙**（額度允許時）。

## 6. Worktree 衛生

- 實作類派工用 `isolation: "worktree"`；merge 回 main 後確認 worktree 已清
  （未清會殘留完整 repo 副本含 sqlite，見 HARNESS_DIAGNOSIS.md 問題 2）。
- 檢查：`git worktree list`；清理：`git worktree remove <路徑> --force` 後
  `git worktree prune`。刪不掉（OneDrive 鎖）→ 回報使用者。
