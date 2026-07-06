# Harness 診斷書 — 本環境最漏 token、最易失焦、最易出錯的三件事

> 建立：2026-07-06（Fable 5 監督 session）。證據全部來自本 repo 的真實事故。
> 後續所有制度檔（DISPATCH / JUDGMENT / PROMPTS / MAINTENANCE）都以此為根據。
> 讀者：未來在此環境工作的任何模型（Sonnet / Opus / Haiku）。

---

## 問題 1：主對話當工人用 → context 淹沒 → 被迫 compaction → 重複勞動

**嚴重度：最高（直接燒錢 + 遺失決策細節）**

### 證據
- V6 開發 session 在主對話內直接讀了 `scripts/agent_arena.py`（約 1,490 行）、
  `scratch/test_arena_final.py`、`scripts/crawler.py`、`docs/REQUIREMENTS_V6.md` 全文，
  又在主對話內跑多輪 live 除錯——結果同一 session **compaction 兩次**。
- Compaction 後的摘要曾遺漏「最終報告還沒發給使用者」這種收尾動作，
  差點讓使用者拿不到結論。

### 為什麼弱模型更容易踩
Sonnet/Haiku 對「先讀全檔再動手」的傾向更強，且不會主動意識到 context 成本。

### 修法（可執行判準）
1. **主對話單次 Read 超過 300 行、或預計要開 3 個以上檔案「看看」→ 停，改派 Explore agent**，
   只要結論與 `檔案:行號`。
2. 主對話只允許讀「這一輪就要 Edit 的那段」——用 Read 的 `offset/limit` 讀目標區段，
   不讀全檔。
3. 批次修改（>3 檔或機械式改動）一律派 subagent，主對話只收驗收結果。
4. 詳細規則見 [DISPATCH.md](DISPATCH.md)。

---

## 問題 2：repo 在 OneDrive 同步目錄裡 → 檔案會被「外力」改動/鎖定 → 模型信了假象

**嚴重度：高（已造成一次規格靜默回退、一次刪檔失敗）**

### 證據
- `docs/REQUIREMENTS_V6.md` 的 R6-1 段落被外力（疑 OneDrive 版本同步）**靜默回退**成
  舊版「兩領域 6 agent」文字；聊天中已定案的是「三領域 9 agents」。
  若模型只信檔案不查 git/聊天紀錄，會照舊規格實作出錯的系統。
- 刪除 0-byte 殘檔 `data/app.db` 時 permission-denied（OneDrive 或程序鎖檔）。
- `data/serenity.sqlite` 與其 `-wal/-shm` 檔在 OneDrive 下同步，有損毀風險。
- `.claude/worktrees/` 殘留兩個舊 worktree（`agent-a42375ec…`、`agent-a90a8d0…`），
  各含完整 sqlite 副本——會誤導全 repo 搜尋（Grep/Glob 撈到過期副本）、浪費同步流量。

### 修法（可執行判準）
1. **git 是唯一事實來源，檔案系統不是。** 發現檔案內容與已知決策矛盾時：
   先跑 `git log --oneline -3 -- <檔案>` 和 `git diff HEAD -- <檔案>`，
   分辨是「有人 commit 過的變更」還是「工作區被外力改動」。外力改動 → 回報使用者再處理。
2. **每次改完文件/規格立即 commit**（本地 commit 即可，不必 push）。未 commit 的內容
   隨時可能被 OneDrive 吃掉。
3. Grep/Glob 搜尋時**排除 `.claude/worktrees/`**（Grep 加 `glob` 參數限定 `scripts/**` 等，
   或搜尋後檢查路徑不含 `worktrees`）。
4. 刪檔遇 permission-denied：不要重試超過一次，改回報使用者手動刪（OneDrive/程序鎖檔，
   模型解不了）。
5. （需使用者決定）長期解法：把 repo 搬出 OneDrive，或把 `data/`、`.claude/` 設為
   OneDrive 排除目錄。見 LETTER.md 第 1 點。

---

## 問題 3：Windows/PowerShell 陷阱 + 驗證未自動化 → 每個坑重複燒 2–5 個 turn

**嚴重度：中高（單次損失小，但每個新 session 都重踩）**

### 證據（每一條都真實發生過）
- 主控台 cp950 編碼：python 印 `≈`、中文字元 → `UnicodeEncodeError` 崩潰。
- PowerShell 5.1 沒有 `&&`／`||`，inline `python -c "…引號…"` 會被解析器咬碎。
- `New-Item -Force` 會清空既有檔案內容（truncate）。
- 測試都在 `scratch/` 用手動跑（`test_arena_final.py` 等），沒有 pytest/CI（ROADMAP D-1
  未動工）——弱模型容易「宣稱完成但沒跑測試」，因為跑測試要記得一堆環境咒語。

### 修法（可執行判準）
1. **跑 python 一律加 UTF-8 咒語**（寫進 CLAUDE.md 常用指令區）：
   ```powershell
   $env:PYTHONIOENCODING = "utf-8"; python scripts\xxx.py
   ```
   或直接用 Bash 工具跑：`PYTHONIOENCODING=utf-8 python scripts/xxx.py`。
2. **含引號/多行的 python one-liner 一律用 Bash 工具**，不用 PowerShell。
3. 驗收鐵律：**沒有貼出測試的實際輸出（含通過數字），就不准說「完成」**。
   arena 相關改動必跑：
   ```bash
   PYTHONIOENCODING=utf-8 python scratch/test_arena_final.py   # 期望 70/70, exit 0
   ```
4. 中期解法：把 `scratch/test_*.py` 轉 pytest + GitHub Actions（ROADMAP D-1），
   讓「跑測試」變成一條無腦指令。

---

## 附註：第 4 名（未列前三，但要知道）

- **LLM 輸出當可信輸入**：live Gemini 曾自創欄位名（`sym/type/qty`），引擎靜默丟單，
  9 個 agent 的買單無痕消失。教訓已修（schema 釘死 + 畸形單記拒單），但這是一個
  **類別**：任何解析 LLM 輸出的地方都要有「畸形 → 顯式記錄，永不靜默丟棄」。
- **Gemini 免費額度**：429 是常態。測試一律用 StubBackend；批次生成排在額度重置
  （太平洋時間午夜）之後。
