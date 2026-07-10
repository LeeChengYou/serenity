# Serenity Signal — 工作計畫與排程規範

> 版本：v1.0 | 建立：2026-07-04 | 維護者：Fable（監督者）
> 狀態標記：⬜ 待辦 ｜ 🔄 進行中 ｜ ✅ 完成 ｜ ⏸ 等待外部條件

---

## 一、每日排程規範（Scheduled Jobs）

系統的驗證閉環依賴以下排程**每天執行**。server 常駐時由內建
背景排程自動執行；server 非常駐時必須用 Windows 工作排程器補上。

### 排程定義

| 任務 | 指令 | 頻率 | 目的 |
|------|------|------|------|
| J-1 價格增量更新 | `python scripts/ingest.py prices` | 每日 06:30（台北，美股收盤後） | 增量抓最新收盤價（冪等，已是最新會自動跳過） |
| J-2 訊號快照 | `python scripts/server.py --snapshot-once` | 每日 06:40（J-1 之後） | 79 檔訊號落地 `signal_history`，累積命中率樣本 |
| J-3 StockTwits 情緒 | `python scripts/ingest.py stocktwits` | 每日 06:50 | 群眾情緒更新（免費 API，404 自動跳過） |
| J-4 X 貼文抓取 | `python scripts/crawler.py refresh-cookies && python scripts/ingest.py fetch-x` | 每週一次 | **需先執行一次 `crawler.py login` 設定瀏覽器 session** |
| **J-5 新聞抓取** | `python scripts/ingest.py news` | 每日 07:00 | Google News RSS（個股）+ CNBC/CNN/Google News（宏觀）；冪等 url 去重 |
| **J-6 基本面更新** | `python scripts/ingest.py fundamentals` | 每週一 07:10 | Yahoo quoteSummary P/E、營收、市值等；Yahoo 封鎖時自動降級 yfinance |
| **J-7 分析師預估** | `python scripts/ingest.py estimates` | 每週一 07:20 | EPS 預估/評級/目標價（yfinance） |
| **J-8 基準指數** | `python scripts/ingest.py benchmarks` | 每日 07:30 | SPY/SOXX/QQQ，供市場情境（regime）判斷 |
| **J-9 專家觀點** | `python scripts/crawler.py fetch-sources` | 每週一 07:40 | SEC EDGAR 13F 持倉變化（官方申報，45 天延遲）|
| **J-10 Arena 日循環** | `python scripts/agent_arena.py daily` | 每日 08:00（J-8 之後） | 撮合昨日 pending 單 → 生成簡報 → 9 agents 決策 → 記錄 NAV |
| **J-11 Arena 月度結算** | `python scripts/agent_arena.py monthly` | 每月 1 日 08:30 | 月度績效結算 + 反思 + 策略卡迭代 |
| **J-12 每日健康檢查** | `python scripts/daily_check.py repair` | 每日 09:00（J-10 之後） | 十項資料域新鮮度檢查 + 自動修復斷點；失敗項依序重跑對應 ingest/arena 指令並輸出終檢報告 |

### Windows 工作排程器註冊指令（由使用者執行）

以系統管理員開 PowerShell，逐條執行（路徑按實際 Python 調整）：

```powershell
$py = "python"
$repo = "C:\Users\Jeff\OneDrive\桌面\git_repo\serenity"

schtasks /Create /TN "Serenity\J1-prices"     /TR "$py $repo\scripts\ingest.py prices"          /SC DAILY /ST 06:30 /F
schtasks /Create /TN "Serenity\J2-snapshot"   /TR "$py $repo\scripts\server.py --snapshot-once" /SC DAILY /ST 06:40 /F
schtasks /Create /TN "Serenity\J3-stocktwits" /TR "$py $repo\scripts\ingest.py stocktwits"      /SC DAILY /ST 06:50 /F
schtasks /Create /TN "Serenity\J5-news"        /TR "$py $repo\scripts\ingest.py news"            /SC DAILY /ST 07:00 /F
schtasks /Create /TN "Serenity\J6-fundamentals" /TR "$py $repo\scripts\ingest.py fundamentals"  /SC WEEKLY /D MON /ST 07:10 /F
schtasks /Create /TN "Serenity\J7-estimates"    /TR "$py $repo\scripts\ingest.py estimates"      /SC WEEKLY /D MON /ST 07:20 /F
schtasks /Create /TN "Serenity\J8-benchmarks"   /TR "$py $repo\scripts\ingest.py benchmarks"          /SC DAILY  /ST 07:30 /F
schtasks /Create /TN "Serenity\J4-refresh-x"    /TR "$py $repo\scripts\crawler.py refresh-cookies && $py $repo\scripts\ingest.py fetch-x" /SC WEEKLY /D MON /ST 07:35 /F
schtasks /Create /TN "Serenity\J9-fetch-sources" /TR "$py $repo\scripts\crawler.py fetch-sources"       /SC WEEKLY /D MON /ST 07:40 /F
schtasks /Create /TN "Serenity\J10-arena-daily"  /TR "$py $repo\scripts\agent_arena.py daily"            /SC DAILY  /ST 08:00 /F
schtasks /Create /TN "Serenity\J11-arena-monthly" /TR "$py $repo\scripts\agent_arena.py monthly"         /SC MONTHLY /D 1 /ST 08:30 /F
schtasks /Create /TN "Serenity\J12-daily-check"  /TR "$py $repo\scripts\daily_check.py repair"           /SC DAILY  /ST 09:00 /F
```

### 使用說明（首次設定）

**X 貼文自動刷新（J-4）需先執行一次登入：**

```powershell
python scripts/crawler.py login
```

瀏覽器會開啟 x.com，請手動完成登入後關閉視窗。Session 儲存於
`data/browser_profile/`（已 gitignore，不含密碼）。之後 J-4 排程
即可自動呼叫 `crawler.py refresh-cookies` 刷新 cookie，無需再次手動登入。

**SEC EDGAR 13F 觀點（J-9）無需任何登入，可直接執行：**

```powershell
python scripts/crawler.py fetch-sources
```

### 注意事項

- **J-4 已升級為半自動**：`crawler.py refresh-cookies` 用
  Playwright 持久 session 自動刷新 x_curl/ 的 cookie，無需手動複製。
  首次需執行 `crawler.py login` 設定 session（見使用說明）。
  Playwright 未安裝時降級輸出 zh-TW 安裝指引並以 exit code 非 0 退出。
- 排程時間假設美股常規時段收盤（台北 04:00/05:00）後執行；
  若遇美股假日，J-1 冪等跳過，無副作用。
- 快照斷檔的後果：拉回策略驗證（需 n≥30）延後結論。

---

## 二、工作計畫

### Phase A — 維運止血（本週）

| # | 項目 | 狀態 | 負責 | 備註 |
|---|------|------|------|------|
| A-1 | GitHub PR 合併（wave4 分支含全部 13 commits） | ⏸ | **使用者** | 等網頁介面驗收通過後合併 |
| A-2 | 註冊 Windows 排程 J-1~J-3 | ⏸ | **使用者** | 指令見上；或改為 server 常駐 |
| A-3 | 更新 X curl cookie、補抓 6/29 之後貼文 | ⬜ | 使用者+監督者 | cookie 需使用者從瀏覽器複製 |
| A-4 | 批次生成 AI 評分卡 3→30 檔 | ⬜ | 監督者 | Gemini 費用約數美元；PR 合併後執行 |

### Phase B — 驗證閉環（本月）

| # | 項目 | 狀態 | 依賴 | 備註 |
|---|------|------|------|------|
| B-1 | 命中率儀表板：前端顯示「30 天前訊號 vs 實際」 | ⬜ | signal_history ≥30 天 | 對用戶最有說服力的誠實功能 |
| B-2 | 拉回變體重跑（n=7→30+ 後下結論） | ⏸ | 快照累積至 ~8 月中 | `backtest_multiwindow.py --pullback` |
| B-3 | 交易成本建模（滑價/點差敏感度） | ⬜ | 無 | 診斷書 D-8，唯一未動工診斷項 |
| B-4 | 閾值掃描（score≥70、RSI 65/40 的實證最優值） | ⬜ | 無 | 用既有橫斷面框架 |
| B-5 | OVERBOUGHT 動能訊號的空頭窗口驗證 | ⏸ | 需含空頭期的資料 | 目前僅多頭環境驗證 |

### Phase C — 產品功能（下月起，按投入產出比排序）

| # | 項目 | 狀態 | 備註 |
|---|------|------|------|
| C-1 | 自選股/持倉 + 損益（watchlist/holdings 表 + UI） | ⬜ | SPEC F-04，留存性最高 |
| C-2 | 警報系統（價格/訊號變化 → toast，後續 Telegram/LINE） | ⬜ | SPEC F-09 |
| C-3 | 基本面整合（Yahoo quoteSummary：P/E、營收增速） | ⬜ | SPEC F-10，補評分卡估值因子 |
| C-4 | 相對強弱 vs SOXX/SMH | ⬜ | SPEC F-08，濾假強勢 |
| C-5 | 多股票比較視圖 /compare | ⬜ | SPEC F-12.4 |
| C-6 | 多 X 帳號三角驗證 | ⬜ | 打破單一帳號依賴 |

### Phase E — AI 經理人競技場（V6，規格見 REQUIREMENTS_V6.md）

| # | 項目 | 狀態 | 備註 |
|---|------|------|------|
| E-1 | P1 引擎骨架：schema、簡報生成器、交易引擎、GeminiBackend、單 agent 一日循環 | ✅ | 驗收測試 70/70（scratch/test_arena_final.py） |
| E-2 | P2 九 agent 日循環：3 領域 × 3 風格、J-10 排程 | ✅ | live 首跑 2026-07-02 完成，9 筆決策入庫；robotics 池擴充（ISRG 等）待辦 |
| E-3 | P3 月度閉環：結算/公開信/反思/策略卡迭代、J-11 排程 | ✅ | 引擎+測試完成；首次 live 月結 2026-08-01（J-11） |
| E-4 | P4 前端競技場頁 + API 四端點 | ✅ | AntigravityBackend adapter 留待後續 |

### Phase D — 工程品質（持續）

| # | 項目 | 狀態 | 備註 |
|---|------|------|------|
| D-1 | scratch/ 測試轉 pytest + GitHub Actions CI | ⬜ | 防回歸 |
| D-2 | 修快照 RSI 解析脆弱性（從條件文字撈→直接回傳欄位） | ⬜ | 工程師 B 自標風險 |
| D-3 | VPS 部署（nginx+systemd）供手機隨時存取 | ⬜ | 可選 |

---

## 二之一、已知問題（待修，2026-07-06 記錄）

| # | 問題 | 現象 | 初步方向 |
|---|------|------|----------|
| K-1 | `crawler.py login` 被 X 安全警告擋下 | 「這個瀏覽器或應用程式可能有安全疑慮，建議您改用其他瀏覽器」，無法完成登入 | Playwright 的 Chromium 被 X 偵測為自動化瀏覽器；候選解法：改用 channel="msedge"/"chrome" 系統瀏覽器、補 user-agent 與 navigator.webdriver 隱藏 |
| K-2 | 前端「專家觀點」卡與「譯 中」翻譯按鈕未顯示 | `/api/expert-views` 有 87 筆資料、`/api/translate` 後端正常，但網頁上看不到卡片與按鈕 | 疑似瀏覽器快取舊版 index.html/app.js，或選中的個股無 13F 對應（卡片無資料時設計為隱藏）；待以 DevTools 確認 DOM 與 fetch 結果 |

---

## 二之二、桌面版打包（V7 §3）

### build 指令

```powershell
# 確認 pywebview 與 pyinstaller 已安裝（沒有就印安裝指令退出）
pip install -r requirements-desktop.txt

# 執行打包（輸出 dist/Serenity/Serenity.exe）
PowerShell -File scripts\build_desktop.ps1
```

### SERENITY_HOME 說明

桌面版（frozen）執行時，所有使用者資料存放在 `SERENITY_HOME`：

| 平台 | 預設路徑 |
|------|---------|
| Windows | `%LOCALAPPDATA%\Serenity\` |
| macOS/Linux | `~/.serenity/` |

可用環境變數 `SERENITY_HOME` 覆蓋。`SERENITY_HOME/serenity.sqlite` 是主資料庫；
`SERENITY_HOME/config.json` 存 Gemini API key 與模型設定。

### X 抓取不隨包（開發者功能）

X 貼文抓取（`scripts/crawler.py`、`x_curl/` cookies）屬開發者功能，
**不打包進散佈版**。一般使用者靠 Google News RSS 取得新聞資料；
競技場模式在無推文資料時自動降級使用新聞/基本面摘要。

---

## 三、決策里程碑

| 日期 | 事件 | 決策 |
|------|------|------|
| PR 合併日 | 使用者驗收網頁介面 | 合併 → Phase A 全面開工 |
| ~2026-08-15 | 拉回變體樣本足夠（n≥30） | 買進訊號最終形態定案 |
| 空頭窗口出現後 | OVERBOUGHT 驗證完成 | 動能訊號是否納入 |

## 四、驗收原則（所有 Phase 通用）

1. 零捏造數據——所有數字可追溯 SQLite 真實列
2. 零 look-ahead——回測沿用 `evaluate_symbol_at_cutoff` 紀律
3. 冪等遷移、None 安全、API 失敗優雅降級
4. 誠實輸出——樣本不足標 insufficient；結論被新證據推翻時
   立即更新 `_RELIABILITY_NOTE` 與 `docs/VALIDATION.md`
