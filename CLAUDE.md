# Serenity Signal — CLAUDE.md（每個 session 自動載入）

本地股票訊號儀表板：X 貼文/新聞/基本面 → SQLite → 訊號評分 → 儀表板；
外加 AI 經理人競技場（9 個 paper-trading agent）。**回覆使用者一律用繁體中文（zh-TW）。**

## 鐵律（違反任何一條 = 任務失敗，無例外）

1. **零捏造數據**：所有數字必須可追溯到 `data/serenity.sqlite` 真實列。缺資料就是
   NULL / 標「insufficient」，不准補假值。
2. **零 look-ahead**：回測與 agent 簡報只能用 `as_of` 當日（含）以前的資料。
3. **只做 paper trading**：競技場是虛擬資金，永遠不接真實券商、不下真單。
4. **git 只推自己的 fork（origin = LeeChengYou/serenity）**：絕不對 upstream
   （haskaomni/serenity）發 PR 或 push。曾發生誤發 upstream PR 造成使用者真實焦慮。
5. **機密不落 git 不落 log**：`.env`（API keys）、`x_curl/`（X session cookies）、
   `data/browser_profile/` 永不 commit、永不印進輸出。
6. 不破壞 `data/serenity.sqlite` 與 `data/raw/`；要動資料先備份或先問。

## 環境陷阱（每條都真實踩過，詳見 docs/agents/HARNESS_DIAGNOSIS.md）

- **repo 在 OneDrive 內**：檔案可能被同步「靜默回退」或鎖住。以 git 為事實來源；
  改完文件立即本地 commit；刪檔 permission-denied 就回報使用者，別硬試。
- **PowerShell 5.1**：沒有 `&&`；含引號的 python one-liner 會碎掉 → 用 Bash 工具。
- **cp950 主控台**：跑 python 前設 `$env:PYTHONIOENCODING = "utf-8"`（Bash 則
  `PYTHONIOENCODING=utf-8` 前綴），否則印中文/特殊符號會崩。
- **DB 路徑是 `data/serenity.sqlite`**，不是 `data/app.db`（那是已 gitignore 的殘骸）。
- **搜尋時排除 `.claude/worktrees/`**（殘留舊副本，會誤導）。
- **Gemini 免費額度會 429**：測試一律用 StubBackend；批次 AI 生成排太平洋午夜後。

## 常用指令

```powershell
# 啟動儀表板（server.py 改動需重啟；dashboard/ 靜態檔不用，叫使用者 Ctrl+F5 即可）
python scripts\server.py --port 8787        # http://127.0.0.1:8787

# 競技場驗收測試（改 agent_arena.py / server.py arena 相關後必跑）
# 通過標準：0 failed、exit 0（案例數會演進；2026-07-06 實測為 70/70）
$env:PYTHONIOENCODING = "utf-8"; python scratch\test_arena_final.py

# 語法快檢
python -m py_compile scripts\ingest.py scripts\server.py scripts\agent_arena.py

# 每日資料管線（完整排程定義見 docs/ROADMAP.md 第一節 J-1~J-11）
python scripts\ingest.py prices
python scripts\agent_arena.py daily
```

## 完成的定義（宣稱「完成」前自查）

- 測試實際跑過且輸出貼在回覆裡（數字，不是「應該會過」）。
- 改了 server.py → 重啟過 server 並 curl 過受影響端點。
- 改了 dashboard/ → 告知使用者 Ctrl+F5（瀏覽器快取曾造成「功能沒顯示」誤報，見 K-2）。
- 文件/規格改動 → 已本地 commit（OneDrive 防回退）。

## 路由（先讀對檔案再動手）

| 情境 | 讀這個 |
|------|--------|
| 要派 subagent / 選模型 / 驗收別人的工作 | `docs/agents/DISPATCH.md` |
| 拿不準「該不該升級模型 / 算不算完成 / 該不該問使用者」 | `docs/agents/JUDGMENT.md` |
| 要寫派工 prompt（搜尋/實作/重構/研究/審查） | `docs/agents/PROMPTS.md` |
| 要更新 docs/agents/ 這套制度檔 | `docs/agents/MAINTENANCE.md` |
| 新 session 起手、想了解環境背景 | `docs/agents/LETTER.md` |
| 排程任務、工作計畫、已知問題（K-1 X 登入、K-2 前端快取） | `docs/ROADMAP.md` |
| 競技場 V6 規格（注意：R6-1 曾被 OneDrive 回退，正確為三領域 9 agents） | `docs/REQUIREMENTS_V6.md` |
| 產品功能規格 / 驗證方法論 | `docs/SPEC.md`、`docs/VALIDATION.md` |
| 資料表結構、pipeline 細節 | `AGENTS.md`（歷史文件，指令部分以本檔為準） |

## 工作模式（使用者已定案的偏好）

- 多步驟工程：**主對話當監督者，派 Sonnet subagent 到隔離 worktree 實作**；
  先寫規格與驗收測試，再派工；逐行審 diff；用真實 DB 資料驗證；本地 merge 到 main。
- 使用者重視誠實勝過華麗：樣本不足就說不足，結論被推翻就更新文件。
