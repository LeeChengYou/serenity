# Serenity Signal Dashboard

本地優先的股票投研訊號平台：X 貼文／新聞／基本面 → SQLite → 技術指標與訊號評分 → 投研儀表板，
外加 **AI 經理人競技場**（9 個 paper-trading agent）與可散佈的**桌面應用版**。
（本倉庫為 [haskaomni/serenity](https://github.com/haskaomni/serenity) 的 fork，已大幅擴充。）

![Serenity dashboard screenshot](docs/assets/serenity-dashboard.png)

> 本專案僅用於研究和可視化呈現，競技場為虛擬資金 paper trading，不構成任何投資建議。

## 功能總覽

| 領域 | 內容 |
|------|------|
| 投研儀表板 | K 線＋EMA20/50＋布林帶＋成交量、RSI/MACD 副圖、訊號評分與歷史、今日訊號分布列、訊號命中率統計（含樣本數警語）、市場情境儀（SPY/QQQ/SOXX EMA200）、訊號轉折記錄、財報倒數徽章、自訂觀察清單（空 DB 出廠內建 20 檔種子） |
| 供應鏈瓶頸記分卡 | Gemini 生成的八維定性評分（Chart.js 雷達圖）＋證據筆記＋版本歷史 |
| AI 投研對話 | 多模型 Gemini 對話、主題語意 RAG（自動關聯個股與價格）、長期記憶（自動提煉＋時間衰減）、中英翻譯 |
| 資料面 | Yahoo 日線價格（增量同步）、Google News/CNBC/CNN 新聞、StockTwits 群眾情緒、基本面與分析師預估、SEC EDGAR 13F 專家觀點、X 貼文 cashtag 擷取（開發者功能） |
| AI 經理人競技場 | 9 個不同策略的 Gemini agent 每日決策 paper trading：撮合、NAV 曲線、排行榜、交易日誌、月度反思與策略卡迭代 |
| 維運 | 儀表板內建資料時效徽章（`/api/health` 十項自檢＋一鍵補抓＋背景每小時自動補抓安全域）、`daily_check.py` CLI 健康檢查＋斷點修復、`job_runs` 執行紀錄、`/monitor.html` AI 呼叫監控面板、Windows schtasks 每日排程（J-1～J-12） |
| 散佈 | 應用內 ⚙ 設定視窗（使用者自填 Gemini API key，免 .env）、pywebview 桌面殼、PyInstaller 打包成 `Serenity.exe`、PWA（手機加入主畫面）＋遠端存取 token 認證（非 localhost 綁定強制，fail-secure） |

## 系統需求

- Python 3.10+（Windows 上以 3.14 實測）
- Gemini API key（[Google AI Studio](https://aistudio.google.com/apikey) 免費申請；最多可設 4 把做 429 failover）
- Windows 主控台請先設 `$env:PYTHONIOENCODING = "utf-8"`（cp950 印中文會崩）

## 安裝與啟動

### 網頁版（開發模式）

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 啟動儀表板（內建 12 小時增量價格排程）
python scripts\server.py --port 8787
# 瀏覽器開 http://127.0.0.1:8787 ；監控面板在 /monitor.html
```

首次啟動會自動彈出 ⚙ 設定視窗，貼上 Gemini API key 即可（也支援傳統 `.env`：
`GEMINI_API_KEY`、`GEMINI_API_KEY_2..4`、`GEMINI_MODEL` 等；環境變數優先於設定檔）。
設定檔位置：`%LOCALAPPDATA%\Serenity\config.json`。

### 桌面版

```powershell
# 開發模式開原生視窗
pip install pywebview
python -m serenity.desktop                 # 自動挑空閒 port
python -m serenity.desktop --smoke         # headless 自檢（CI 用）

# 打包成可散佈的 exe（onedir）
pip install -r requirements-desktop.txt
.\scripts\build_desktop.ps1                # 產出 dist\Serenity\Serenity.exe
```

打包版資料庫與設定放在 `%LOCALAPPDATA%\Serenity\`；首次啟動空資料庫時，
儀表板會引導「填 key → 一鍵抓取初始資料（prices/benchmarks/news）」。
X 貼文抓取不隨包散佈（需要瀏覽器 cookies，屬開發者功能）。

## 每日資料管線

完整排程定義（J-1～J-12，含 schtasks 註冊指令）見 `docs/ROADMAP.md` 第一節。手動執行：

```powershell
# 資料擷取（scripts\ingest.py 子命令）
python scripts\ingest.py prices            # J-1 日線價格增量更新
python scripts\ingest.py stocktwits        # J-3 StockTwits 情緒
python scripts\ingest.py news              # J-5 新聞（個股＋宏觀）
python scripts\ingest.py fundamentals      # J-6 基本面（每週）
python scripts\ingest.py estimates         # J-7 分析師預估（每週）
python scripts\ingest.py benchmarks        # J-8 SPY/SOXX/QQQ 基準
python scripts\ingest.py fetch-x --max-pages 10   # J-4 X 貼文（需 x_curl/ cookies）
python scripts\ingest.py stats             # 資料庫統計總覽

# 訊號快照與爬蟲
python scripts\server.py --snapshot-once   # J-2 當日訊號落地 signal_history
python scripts\crawler.py refresh-cookies  # 刷新 X session（Playwright）
python scripts\crawler.py fetch-sources    # J-9 SEC EDGAR 13F 專家觀點

# AI 競技場
python scripts\agent_arena.py daily                     # J-10 日循環（撮合→簡報→9 agents 決策→NAV）
python scripts\agent_arena.py daily --as-of 2026-07-08  # 補跑指定日
python scripts\agent_arena.py monthly                   # J-11 月度結算＋反思

# 健康檢查與修復（J-12）
python scripts\daily_check.py check          # 十項資料新鮮度檢查（--json 機器可讀）
python scripts\daily_check.py repair         # 只重跑不健康的環節，修完終檢
python scripts\daily_check.py run            # 完整跑當日全流程並回報斷點
python scripts\daily_check.py run --dry-run  # 只列計畫不執行
python scripts\catchup.py                    # 缺漏多日時的一鍵補跑（含 Arena 回填）
python scripts\batch_scorecards.py --dry-run # J-13 批次記分卡生成（先看計畫；實跑建議台北 15:10 避 429）
python scriptsatch_scorecards.py --dry-run   # J-13 批次記分卡生成（先看計畫；實跑建議台北 15:10 避 429）
```

## 測試

所有驗收測試在 `scratch/`，通過標準一律 **0 failed、exit 0**（案例數會隨功能演進）：

```powershell
$env:PYTHONIOENCODING = "utf-8"

python scratch\test_arena_final.py       # 競技場 V6 驗收（StubBackend，零 Gemini 呼叫）
python scratch\test_daily_check.py       # daily_check 健康檢查／修復
python scratch\test_server_refactor.py   # 24 個 API 端點 vs 黃金基準（server 拆模組的行為對照）
python scratch\test_settings.py          # 設定系統／API key 遮罩（假 key，零外洩驗證）
python scratch\test_desktop.py           # 桌面殼 --smoke／in-process 管線／bootstrap API
python scratch\test_health.py            # 資料時效自檢（/api/health＋自動補抓計畫）
python scratch\test_product_p0.py        # 觀察清單／訊號分布／樣本警語／批次記分卡
python scratch\test_pwa_auth.py          # PWA＋遠端 token 認證（fail-secure）
python scratch\test_indicators.py        # 技術指標單元測試
python scratch\test_signal_rsi_field.py  # 訊號欄位迴歸測試
python scratch\test_fund_pool.py         # 模擬資金池（下單／撮合／會診／outcome 回填）
python scratch\test_chat_market.py       # 聊天室市場總覽 context
python scratch\test_local_llm.py         # 本地 LLM（假 Ollama server，不需真裝）
python scratch\test_tw_phase1.py         # 台股 Phase 1（watchlist／region／拒單邊界）
python scratch\test_deep_dive.py         # 個股深度研究（數值計算／報告落庫／outcome 回填）
python scratch\test_arena_local.py       # 競技場 LocalBackend（假 Ollama server）
python scratch\test_tw_directory.py      # 台股全目錄／搜尋／種子預載（假 TWSE/TPEx fixture）
python scratch\test_news_page.py         # 新聞流 API／新聞·專家頁

# 語法快檢
python -m py_compile scripts\ingest.py scripts\server.py scripts\agent_arena.py scripts\daily_check.py

# 重構前重新擷取 API 黃金基準（僅在「有意」改變 API 形狀時執行）
python scratch\test_server_refactor.py --capture
```

改動 `serenity/`（server 相關）後必跑 `test_server_refactor.py`；
改動 `agent_arena.py` 後必跑 `test_arena_final.py`；
測試一律使用 StubBackend／假 key，不消耗 Gemini 額度。

## 專案結構

```
serenity/                # 後端套件（api/ 路由分發、services/ 業務邏輯、db/keypool/gemini/config）
  desktop.py             # pywebview 桌面殼入口（python -m serenity.desktop）
  app.py                 # 網頁版伺服器入口（由 scripts/server.py shim 呼叫）
scripts/                 # CLI：ingest / server(shim) / agent_arena / daily_check / catchup / crawler
dashboard/               # 前端（vanilla JS，零 build step）：index.html / app.js / monitor.html
packaging/               # PyInstaller spec 與打包入口
skills/serenity-stock-scorer/   # 單檔量化評分 skill
scratch/                 # 驗收測試與一次性檢查腳本
docs/                    # ROADMAP（排程與已知問題）、REQUIREMENTS_V6/V7（規格）、SPEC、VALIDATION
data/serenity.sqlite     # 本機資料庫（gitignored）；打包版在 %LOCALAPPDATA%\Serenity\
```

## X 貼文抓取（開發者功能）

`ingest.py fetch-x` 讀取 `x_curl/` 內從 Chrome DevTools 複製的 GraphQL curl。
首次設定：登入 X → 開 `https://x.com/aleabitoreddit` → DevTools Network 篩 `UserTweets` →
右鍵 `Copy as cURL`，分別存成：

```text
x_curl/UserTweets.curl
x_curl/UserTweetsAndReplies.curl
x_curl/UserSuperFollowTweets.curl
```

`x_curl/*.curl` 含登入 cookie/token，已被 `.gitignore` 忽略——**切勿提交或分享**。
session 過期時可用 `python scripts\crawler.py refresh-cookies` 自動刷新（已知 X 會攔自動化登入，見 `docs/ROADMAP.md` K-1）。

## 量化評分 Skill

```powershell
python skills\serenity-stock-scorer\scripts\score_serenity_stock.py NVDA --pretty
```

基於本機 SQLite 語料對單一 ticker 輸出 0-100 訊號分；DB 在別處時傳 `--db` 或設 `SERENITY_DB_PATH`。

---

## English Overview

Serenity is a local-first stock research platform: it ingests X posts, news, fundamentals,
and prices into SQLite, computes technical indicators and signal scores, and serves a web
dashboard (`python scripts\server.py --port 8787`) — plus an AI manager arena where nine
Gemini-powered agents paper-trade daily. It ships as a distributable desktop app
(`python -m serenity.desktop`; build with `scripts\build_desktop.ps1`) with an in-app
settings window where users paste their own Gemini API key (no `.env` needed).
Daily pipeline health is guarded by `python scripts\daily_check.py check|repair|run`.
All acceptance tests live in `scratch/` (`test_arena_final`, `test_daily_check`,
`test_server_refactor`, `test_settings`, `test_desktop`) and must exit 0 with 0 failed.
Research/visualization only — not financial advice.
