# Serenity Signal — 股票分析推薦平台 完整規格書

> 版本：v1.0 | 日期：2026-07-04 | 作者：多 Agent 協作分析

---

## 目錄

1. [專案現況評估](#1-專案現況評估)
2. [功能缺口總覽 (4 角度交叉分析)](#2-功能缺口總覽)
3. [核心功能詳細規格](#3-核心功能詳細規格)
4. [技術架構規格](#4-技術架構規格)
5. [分期開發計畫](#5-分期開發計畫)
6. [立即修復清單 (Quick Wins)](#6-立即修復清單)

---

## 1. 專案現況評估

### 1.1 已實作功能

| 分類 | 功能 | 狀態 |
|------|------|------|
| 資料來源 | X 帳號貼文抓取 (@aleabitoreddit) | ✅ 但僅單一帳號 |
| 資料來源 | Yahoo Finance 日 K OHLCV | ⚠️ 只存 close + volume，缺 open/high/low |
| 資料來源 | TradingView 補充回填 | ✅ |
| 儲存 | SQLite WAL 模式 | ✅ |
| 評分 | 定量 X 語料評分 (0-100) | ✅ |
| 評分 | 定性供應鏈瓶頸評分卡 (8 因子) | ✅ |
| 前端 | 三欄佈局（股票清單/圖表/AI 聊天）| ✅ |
| 前端 | 價格圖 + 提及標記 | ✅ |
| 前端 | 雷達圖評分卡 | ✅ |
| AI | Gemini RAG 多輪聊天 | ✅ |
| AI | AI 自動生成評分卡 | ✅ |
| AI | 長期記憶系統 | ⚠️ 有 decay 計算 bug |
| 監控 | AI 呼叫監控頁面 | ✅ |

### 1.2 關鍵缺陷（技術架構師發現）

```
[嚴重] prices 表缺 open/high/low → 所有技術指標無法計算
[嚴重] X 資料依賴手動複製 curl → session cookie 失效即全停
[嚴重] memory decay SQL 括號錯誤 → server.py:685
        錯誤: julianday('now') - julianday(updated_at) * 0.1
        正確: (julianday('now') - julianday(updated_at)) * 0.1
[中等] 背景排程無持久化 → server 重啟後計時器歸零
[中等] Gemini 呼叫同步阻塞主執行緒
[中等] 無驗證層 → API 完全公開，無限制費用
```

---

## 2. 功能缺口總覽

> 4 個專業角度 Agent 交叉分析，共識評分取最高優先

### 2.1 四角度交叉評分矩陣

| 功能 | PM | 技術架構師 | 量化分析師 | UX 設計師 | 綜合優先度 |
|------|:--:|:--------:|:--------:|:--------:|:--------:|
| OHLC 完整化 + 技術指標 | P1 | **P0** | **P1** | P2 | **P0** |
| Memory decay bug 修復 | — | **P0** | — | — | **P0** |
| 多數據源（新增帳號/新聞）| **P1** | P1 | P2 | — | **P1** |
| 評分卡歷史版本化 | **P1** | — | — | P1 | **P1** |
| 個人自選股 + 部位追蹤 | **P1** | — | — | **P1** | **P1** |
| 技術指標買賣訊號 | P1 | P1 | **P1** | — | **P1** |
| URL 路由 + 分享連結 | — | — | — | **P1** | P1 |
| 手機 Bottom Nav | — | — | — | **P1** | P1 |
| 警報系統 | **P1** | P2 | — | P2 | **P2** |
| 基本面資料整合 | P2 | P1 | P2 | — | **P2** |
| 相對強弱 vs 指數 | — | — | **P2** | — | P2 |
| ATR 停損/倉位計算器 | P2 | — | **P2** | — | P2 |
| 回測框架 | P2 | — | P2 | — | P2 |
| 多股票比較視圖 | P2 | — | — | P2 | P2 |
| 結構化買/賣/觀察推薦 | **P1** | — | **P2** | — | P2 |
| StockTwits 情緒數據 | — | P1 | P2 | — | P2 |
| 用戶帳號 + 計費 | **P1** | P3 | — | — | P3 |
| VPS 部署 + nginx | — | P2 | — | — | P3 |

---

## 3. 核心功能詳細規格

### F-01 OHLC 完整化 + 技術指標引擎 【P0】

**背景：** 現有 `prices` 表只有 `close` 和 `volume`，導致所有技術分析無法執行。

**規格：**

#### 3.1.1 資料庫遷移
```sql
ALTER TABLE prices ADD COLUMN open  REAL;
ALTER TABLE prices ADD COLUMN high  REAL;
ALTER TABLE prices ADD COLUMN low   REAL;
```

#### 3.1.2 資料採集修正（`scripts/ingest.py`）
- `fetch_prices()` 已從 Yahoo v8 收到完整 OHLCV，但只存 close+volume
- 修正：同時存入 `open`, `high`, `low`

#### 3.1.3 技術指標計算（`scripts/server.py`）
新增依賴：`pandas-ta`（純 Python，無 C 編譯需求）

| 指標 | 參數 | 用途 |
|------|------|------|
| EMA | 20 日, 50 日 | 趨勢過濾，在兩條 EMA 上方才考慮做多 |
| RSI | 14 日 | 動量門檻，>50 偏多，<30 超賣機會 |
| MACD | 12/26/9 | 趨勢轉折確認 |
| Bollinger Bands | 20 日, 2σ | 波動度與均值回歸 |
| ATR | 14 日 | 必填：停損距離與倉位計算基礎 |
| Volume Ratio | 當日/20日均量 | 提及日成交量確認訊號強度 |

**API 修改：** `/api/symbol/<SYM>` 回傳新增 `indicators` 欄位
```json
{
  "prices": [...],
  "indicators": {
    "ema20": [...], "ema50": [...],
    "rsi14": [...], "macd": [...],
    "bb_upper": [...], "bb_lower": [...],
    "atr14": 3.45, "volume_ratio": 1.82
  }
}
```

**前端：** 在現有 Chart.js 圖下方加第二個 canvas（RSI），EMA 線疊加在主圖

---

### F-02 記憶體 Decay Bug 修復 【P0】

**位置：** `scripts/server.py:685`

**修復：**
```python
# 錯誤（當前）:
WHERE weight - julianday('now') - julianday(updated_at) * 0.1 < 0.1

# 正確:
WHERE weight - (julianday('now') - julianday(updated_at)) * 0.1 < 0.1
```

---

### F-03 評分卡歷史版本化 【P1】

**背景：** 現有 `scorecards` 表用 `symbol` 作 primary key，每次更新覆蓋舊資料。

**新表：**
```sql
CREATE TABLE scorecard_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    final_score REAL NOT NULL,
    verdict     TEXT,
    factors_json TEXT,
    penalties_json TEXT,
    model_used  TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    INDEX idx_scorecard_history_symbol (symbol, created_at DESC)
);
```

**修改流程：** 生成新評分卡前，先將現有評分卡寫入 `scorecard_history`

**前端新增：**
- 評分卡標籤頁上方加 5 點 sparkline 圖（歷史分數趨勢）
- 上升顯示 acid-green，下降顯示 ember-orange
- Hover 顯示該日期的 verdict 文字

---

### F-04 個人自選股 + 部位追蹤 【P1】

**新表：**
```sql
CREATE TABLE watchlist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL UNIQUE,
    added_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE holdings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    shares      REAL NOT NULL,
    cost_basis  REAL NOT NULL,
    currency    TEXT DEFAULT 'USD',
    added_at    TEXT DEFAULT (datetime('now')),
    note        TEXT
);
```

**新 API 端點：**
- `GET /api/watchlist` — 取得自選股清單
- `POST /api/watchlist` — 新增自選股
- `DELETE /api/watchlist/<SYM>` — 移除自選股
- `GET /api/holdings` — 取得持倉清單
- `POST /api/holdings` — 新增持倉
- `PUT /api/holdings/<id>` — 更新持倉
- `DELETE /api/holdings/<id>` — 刪除持倉

**前端 /watchlist 頁面 wireframe：**
```
WATCHLIST                    HOLDINGS
+------------------+  +---------------------------------+
| [+ Add symbol]   |  | [+ Add position]                |
+------------------+  +---------------------------------+
| ★ NVDA  Score:91 |  | NVDA  100 shares @ $125.00      |
|   $138.22 +10.4% |  | Now: $138.22  P&L: +$1,322 (+10.6%) |
| ★ TSM   Score:78 |  | TSM   200 shares @ $165.00      |
|   $172.10 +4.2%  |  | Now: $172.10  P&L: +$1,420 (+4.3%)  |
+------------------+  +---------------------------------+
                       Total unrealized: +$2,742 (+7.4%)
```

---

### F-05 多數據源擴展 【P1】

#### 5.1 StockTwits 情緒數據（零成本，無認證）
```
GET https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json
→ 取 30 則貼文，存入 news_sentiment 表
→ 注入 RAG context，改善 AI 回答品質
```

#### 5.2 Alpha Vantage 新聞情緒（免費 tier 25 req/day）
```
GET https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={symbol}&apikey={key}
→ 存入 news_sentiment 表 (symbol, published_at, headline, sentiment_score, source)
→ 在評分卡 evidence panel 顯示最新 3 則新聞
```

#### 5.3 多 X 帳號支援（`ingest.py` 修改）
- 將 `TARGET_USER_ID` 從單一字串改為清單
- 每個帳號一個 curl 資料夾
- 評分時加入「多帳號同期提及同一股票」權重加成

#### 5.4 SEC EDGAR 重大公告（免費 RSS）
```
GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={symbol}&type=8-K&dateb=&owner=include&count=5&output=atom
→ 存 8-K, 10-Q 標題 + 日期
→ 在圖表上顯示財報標記（不同顏色的點）
```

**新聞情緒表：**
```sql
CREATE TABLE news_sentiment (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    source         TEXT NOT NULL,  -- 'stocktwits', 'alphavantage', 'sec', 'rss'
    published_at   TEXT NOT NULL,
    headline       TEXT,
    sentiment      TEXT,           -- 'Bullish', 'Bearish', 'Neutral'
    sentiment_score REAL,          -- -1.0 to 1.0
    url            TEXT,
    content_snippet TEXT
);
```

---

### F-06 技術指標買賣訊號引擎 【P1】

**規格：** 基於規則的結構化推薦輸出（不用 AI，確定性計算）

**訊號觸發條件：**

| 訊號 | 條件 | 顯示 |
|------|------|------|
| BUY_WATCH | Serenity Score ≥ 70 AND 價格 > EMA20 AND RSI < 65 | 🟢 觀察進場 |
| BUY_TRIGGER | BUY_WATCH + Volume Ratio > 1.5 + MACD 金叉 | 🟢🟢 觸發進場 |
| HOLD | 已進場 AND 價格 > EMA50 AND RSI 50-70 | 🔵 持有 |
| EXIT_ALERT | 價格跌破 EMA50 OR RSI < 40 OR Score 下降 > 15 | 🔴 注意出場 |
| OVERBOUGHT | RSI > 75 AND 近 20 日漲幅 > 30% | ⚠️ 過熱警示 |

**前端顯示（股票資訊區塊下方）：**
```
┌─────────────────────────────────────────┐
│ SIGNAL: 🟢 BUY_WATCH                   │
│                                         │
│ Conditions:                             │
│ [✓] Serenity Score ≥ 70 (84)          │
│ [✓] Price above EMA20 ($132.1)         │
│ [✗] RSI < 65 (currently 71 - wait)    │
│ [✓] Volume OK (1.2x avg)              │
│                                         │
│ Entry Zone:    $128 - $135 (EMA20)     │
│ Stop Loss:     $120.5 (1.5× ATR)      │
│ Risk/Share:    $9.50                   │
│ R:R Ratio:     1:1.8 (Target $152)    │
└─────────────────────────────────────────┘
```

---

### F-07 ATR 停損 + 倉位計算器 【P2】

**計算公式：**
```python
stop_distance = atr_14 * 1.5          # 標準 1.5x ATR 停損
shares = (equity * risk_pct) / stop_distance  # Kelly-inspired sizing
stop_price = entry_price - stop_distance
target_price = entry_price + (stop_distance * rr_ratio)  # 預設 R:R = 1:2
```

**UI：** 在訊號區塊下方加一個小計算器
- Input: 帳戶資金（$）、每筆風險（%）、預計進場價
- Output: 建議股數、停損價位、目標價位、R:R 比率

---

### F-08 相對強弱對比指數 【P2】

**數據：** 自動抓取 SOXX (iShares 半導體 ETF) / SMH 作為基準
- 在 `ingest.py` 的 `prices` 抓取流程中加入 SOXX/SMH

**計算：** `RS_ratio = stock_close / benchmark_close`，以第一次提及日歸一化為 100

**前端：** 主圖右側 Y 軸疊加 RS 線（灰色虛線）
- RS > 100 表示相對跑贏，標示 "Outperforming SOXX"
- RS < 100 表示相對跑輸，標示 "Underperforming SOXX"

---

### F-09 警報系統 【P2】

**新表：**
```sql
CREATE TABLE alert_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    rule_type   TEXT NOT NULL,
    -- 'price_above', 'price_below', 'rsi_overbought', 'rsi_oversold',
    -- 'score_drop', 'mention_spike', 'macd_cross'
    threshold   REAL NOT NULL,
    channel     TEXT DEFAULT 'toast',  -- 'toast', 'email', 'telegram', 'line'
    destination TEXT,
    active      INTEGER DEFAULT 1,
    last_fired  TEXT
);

CREATE TABLE notifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id   INTEGER REFERENCES alert_rules(id),
    symbol    TEXT NOT NULL,
    message   TEXT NOT NULL,
    fired_at  TEXT DEFAULT (datetime('now')),
    read      INTEGER DEFAULT 0
);
```

**警報評估：** 在背景執行緒改為市場時段每 5 分鐘評估一次

**通知渠道優先順序：**
1. **v1:** 前端 Toast 通知（已有 `showToast()` 函數，0 成本）
2. **v2:** Email via Gmail SMTP（stdlib `smtplib`，需用戶填 `.env`）
3. **v3:** Telegram Bot（`python-telegram-bot`，適合台灣市場）
4. **v3:** LINE Notify（台灣最常用通訊 APP）

**UI wireframe（股票列 bell icon）：**
```
[NVDA  84  $138.22]  [🔔]

Popover:
┌──────────────────────────┐
│ Alerts for NVDA          │
│──────────────────────────│
│ [✓] Score drops < 70     │
│ [✓] Price < $120         │
│ [ ] RSI > 80             │
│ [+ Add rule]             │
│ [Save]        [Cancel]   │
└──────────────────────────┘
```

---

### F-10 基本面資料整合 【P2】

**數據來源：** Yahoo Finance quoteSummary API（免費）

**API 呼叫：**
```
https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}
  ?modules=financialData,incomeStatementHistoryQuarterly,defaultKeyStatistics
```

**存儲表：**
```sql
CREATE TABLE fundamentals (
    symbol           TEXT PRIMARY KEY,
    pe_ratio         REAL,
    ev_revenue       REAL,
    gross_margin     REAL,
    revenue_growth   REAL,
    short_float_pct  REAL,
    next_earnings    TEXT,   -- 下次財報日期
    market_cap       REAL,
    updated_at       TEXT
);
```

**整合方式：**
- 每次評分卡生成時自動帶入 `valuation_disconnect` 因子的真實數據
- 在評分卡頁面顯示基本面摘要區塊
- 在 RAG context 中注入基本面數據

---

### F-11 回測框架 【P2】

**腳本：** `scripts/backtest_signals.py`

**邏輯：**
```python
# 對所有 (symbol, mention_date) 計算:
forward_returns = {
    't5':  price(mention_date + 5d)  / price(mention_date) - 1,
    't10': price(mention_date + 10d) / price(mention_date) - 1,
    't20': price(mention_date + 20d) / price(mention_date) - 1,
    't60': price(mention_date + 60d) / price(mention_date) - 1,
}

# 分組:
score_buckets = [(0,40), (40,60), (60,80), (80,100)]
# 輸出: 每個 bucket 的 median return, win_rate, max_drawdown
```

**前端：** 新增「Signal Backtest」區塊在總覽頁
```
Score Bucket  │ Median T+20  │ Win Rate  │ Samples
──────────────┼──────────────┼───────────┼─────────
80-100        │ +12.3%       │  68%      │   47
60-80         │  +5.1%       │  58%      │   93
40-60         │  +1.2%       │  51%      │   62
0-40          │  -3.4%       │  38%      │   28
```

---

### F-12 UX 重構：URL 路由 + 手機支援 【P1】

#### 12.1 URL 路由（無後端修改，純前端）
```javascript
// 選擇股票時
history.pushState({symbol}, '', `/?s=${symbol}&tab=chart`);

// 頁面載入時
const params = new URLSearchParams(location.search);
const initSymbol = params.get('s');
const initTab = params.get('tab') || 'chart';
```

#### 12.2 頂部導航列
```
┌────────────────────────────────────────────────────┐
│ [S] SERENITY    Dashboard  Watchlist  Compare  Alerts │
└────────────────────────────────────────────────────┘
```
高度 52px，深墨色 `#182019`，acid-green 底線標記當前頁

#### 12.3 手機底部導航（< 768px）
```
┌──────────────────────────────┐
│  [active panel content]      │
│                              │
└──────────────────────────────┘
│ [清單] [圖表] [評分] [聊天]  │  ← 44px sticky bar
└──────────────────────────────┘
```

#### 12.4 多股票比較頁（/compare）
- URL: `/compare?a=NVDA&b=TSM`
- 雙欄佈局，共用 X 軸時間範圍選擇
- 因子差異表：自動高亮評分差異 > 1 分的因子
- 兩個雷達圖使用相同刻度方便視覺比對

---

### F-13 Onboarding 新手引導 【P2】

**觸發條件：** `localStorage.getItem('onboarded') === null`

**形式：** 右下角浮動卡片（非全屏 Modal）

```
┌─────────────────────────────────┐
│ 歡迎使用 Serenity Signal         │
│ AI 驅動的供應鏈投資分析工具        │
│                                 │
│ Step 1 of 4                     │
│ [ ] 點擊左側股票清單 → 試試 NVDA  │
│ [ ] 切換到「評分卡」標籤          │
│ [ ] 點擊「生成 AI 分析」          │
│ [ ] 在 AI 聊天問：NVDA 的風險？   │
│                                 │
│ [跳過]              [下一步 →]   │
└─────────────────────────────────┘
```

---

## 4. 技術架構規格

### 4.1 現有架構（維持，僅強化）

```
Browser (Vanilla JS + Chart.js)
    ↕ HTTP
Python ThreadingHTTPServer (server.py)
    ↕
SQLite WAL (serenity.sqlite)
    ↕                    ↕
Yahoo Finance API    Google Gemini API
```

### 4.2 強化架構（Phase 1-2 目標）

```
Browser (Vanilla JS + Chart.js/Lightweight Charts)
    ↕ HTTP
nginx (反向代理 + TLS + 靜態文件)
    ↕
Python ThreadingHTTPServer / FastAPI
    ├── /api/symbol     → SQLite (prices + indicators computed)
    ├── /api/chat       → Gemini API (async thread pool)
    ├── /api/scorecard  → Gemini API (async thread pool)
    ├── /api/watchlist  → SQLite
    ├── /api/alerts     → SQLite
    └── Background Workers:
        ├── 每 5 分鐘: 評估警報規則（市場時段）
        ├── 每 15 分鐘: Yahoo 盤中報價（市場時段）
        ├── 每 12 小時: 日 K 更新 + StockTwits 抓取
        └── 每天: fundamentals 更新

SQLite WAL
├── prices          (OHLCV, 含 open/high/low)
├── prices_intraday (5m OHLCV)
├── news_sentiment  (StockTwits + Alpha Vantage)
├── fundamentals    (基本面)
├── scorecard_history (版本化)
├── watchlist
├── holdings
├── alert_rules
├── notifications
└── 現有表不變
```

### 4.3 新增依賴

| 套件 | 用途 | 安裝 |
|------|------|------|
| `pandas-ta` | 技術指標計算 | `pip install pandas-ta` |
| `pandas` | 資料框操作（pandas-ta 依賴）| 同上自動安裝 |
| `yfinance` | 基本面 + 備用報價 | `pip install yfinance` |

**零新依賴方案（保守路線）：**
- 技術指標：自行用 stdlib `statistics` 模組計算 EMA/RSI/ATR
- StockTwits：`urllib.request`（stdlib，已在用）
- SEC EDGAR：RSS 解析用 `xml.etree.ElementTree`（stdlib）

### 4.4 部署架構（Phase 1 VPS）

```
Hetzner/DigitalOcean VPS ($6/mo, 2 vCPU 2GB)
├── nginx
│   ├── TLS (Certbot Let's Encrypt)
│   ├── 靜態文件服務 (dashboard/)
│   └── 反向代理 → 127.0.0.1:8787
├── systemd service: serenity.service
│   ├── Restart=always
│   └── ExecStart=python /app/scripts/server.py
└── SQLite on persistent volume
    └── 每日 .dump 備份至 Backblaze B2
```

**systemd unit 範例：**
```ini
[Unit]
Description=Serenity Signal Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/app
ExecStart=/app/serenity/bin/python scripts/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 5. 分期開發計畫

### Phase 0 — 緊急修復（1-2 天）

| # | 任務 | 文件 | 複雜度 |
|---|------|------|--------|
| 0.1 | prices 表加 open/high/low 欄位 | ingest.py, server.py | 簡單 |
| 0.2 | 修復 memory decay SQL bug | server.py:685 | 簡單 |
| 0.3 | 修復 app.js dangling code (line 321-328) | dashboard/app.js | 簡單 |

### Phase 1 — 核心分析引擎（2-4 週）

| # | 任務 | 依賴 | 複雜度 |
|---|------|------|--------|
| 1.1 | 技術指標計算 (EMA/RSI/MACD/BBands/ATR) | Phase 0.1 | 中等 |
| 1.2 | 前端圖表：指標疊加 + RSI 子圖 | 1.1 | 中等 |
| 1.3 | 買賣訊號規則引擎 | 1.1 | 中等 |
| 1.4 | 倉位計算器 UI | 1.1 | 簡單 |
| 1.5 | 評分卡歷史版本化 | 無 | 簡單 |
| 1.6 | StockTwits 數據接入 | 無 | 簡單 |
| 1.7 | SEC EDGAR RSS 接入 | 無 | 簡單 |

### Phase 2 — 個人化功能（2-3 週）

| # | 任務 | 依賴 | 複雜度 |
|---|------|------|--------|
| 2.1 | 自選股 watchlist 功能 | 無 | 簡單 |
| 2.2 | 持倉追蹤 + P&L 計算 | 無 | 簡單 |
| 2.3 | URL 路由 + 分享連結 | 無 | 簡單 |
| 2.4 | 手機 Bottom Navigation | 無 | 簡單 |
| 2.5 | 頂部導航列 | 2.3 | 簡單 |
| 2.6 | 警報規則 + Toast 通知 | 1.1 | 中等 |
| 2.7 | 評分卡歷史 Sparkline | Phase 1.5 | 簡單 |

### Phase 3 — 進階分析（3-4 週）

| # | 任務 | 依賴 | 複雜度 |
|---|------|------|--------|
| 3.1 | 基本面資料整合 (Yahoo quoteSummary) | 無 | 中等 |
| 3.2 | 相對強弱 vs SOXX/SMH | Phase 0.1 | 中等 |
| 3.3 | 回測框架 backtest_signals.py | Phase 0.1 | 中等 |
| 3.4 | 多股票比較視圖 (/compare) | 2.3 | 困難 |
| 3.5 | Yahoo 盤中 5 分鐘報價 | Phase 0.1 | 中等 |
| 3.6 | Email/Telegram 警報通知 | 2.6 | 中等 |
| 3.7 | 多 X 帳號支援 | 無 | 中等 |
| 3.8 | Alpha Vantage 新聞情緒 | 無 | 簡單 |

### Phase 4 — 產品化（4-6 週）

| # | 任務 | 依賴 | 複雜度 |
|---|------|------|--------|
| 4.1 | VPS 部署 (nginx + systemd + TLS) | 無 | 中等 |
| 4.2 | Onboarding 新手引導 | Phase 2 完成 | 簡單 |
| 4.3 | 用戶帳號 + JWT 驗證 | 4.1 | 困難 |
| 4.4 | Stripe 計費整合 | 4.3 | 困難 |
| 4.5 | Redis 快取層 | 4.1 | 中等 |
| 4.6 | FastAPI 遷移（可選） | 4.1 | 困難 |
| 4.7 | PostgreSQL + TimescaleDB 遷移 | 4.1 | 困難 |

---

## 6. 立即修復清單

> 以下可在 1 天內完成，無需新功能設計

### Quick Win 1: OHLC Schema + 資料修正

```python
# scripts/ingest.py - fetch_prices() 函數中
# 現有（約 line 334）:
self.db.execute(
    "INSERT OR REPLACE INTO prices (symbol, date, close, volume) VALUES (?,?,?,?)",
    (symbol, date_str, close, volume)
)

# 修改為:
self.db.execute(
    "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
    (symbol, date_str, open_p, high, low, close, volume)
)
```

### Quick Win 2: Memory Decay Bug

```python
# scripts/server.py:685 附近 - 尋找 decay 相關 SQL
# 搜尋: julianday('now') - julianday(updated_at) * 0.1
# 替換為: (julianday('now') - julianday(updated_at)) * 0.1
```

### Quick Win 3: StockTwits 第二數據源

```python
# 新增到 scripts/ingest.py
def fetch_stocktwits(self, symbol: str) -> list:
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    resp = urllib.request.urlopen(url, timeout=10)
    data = json.loads(resp.read())
    results = []
    for msg in data.get('messages', []):
        sentiment = msg.get('entities', {}).get('sentiment', {})
        results.append({
            'symbol': symbol,
            'source': 'stocktwits',
            'published_at': msg['created_at'],
            'headline': msg['body'][:200],
            'sentiment': sentiment.get('basic', {}).get('sentiment', 'Neutral'),
            'url': f"https://stocktwits.com/message/{msg['id']}"
        })
    return results
```

### Quick Win 4: app.js dangling code 修復

在 `dashboard/app.js` line 321 附近，確認 `selectSymbol` 函數的閉括號位置，移除孤立的 dead code。

---

## 附錄 A：4 Agent 交叉意見摘要

| 觀點 | 最強共識 | 分歧點 |
|------|---------|--------|
| **PM** | 單一數據源是最大風險，評分卡版本化是低成本高價值 | 用戶帳號計費比技術層更優先 |
| **技術架構師** | OHLC schema 修復是所有功能的先決條件；memory decay 是 P0 bug | 傾向早期引入 pandas-ta 而非自寫指標 |
| **量化分析師** | 回測框架是信任建立的關鍵；ATR 倉位計算才是真正護本工具 | 相對強弱比評分卡更能篩選優質進場 |
| **UX 設計師** | URL 路由是免費且解鎖後續所有功能；手機支援是留存關鍵 | 密度切換（compact/comfortable）比新功能更能提升每日使用感 |

**4 角度唯一共識：OHLC 完整化必須最先做，其他所有功能都依賴它。**

---

*文件生成自 4 專業 Agent（PM / Technical Architect / Quantitative Analyst / UX Designer）並行分析，最終由主 Agent 整合。*
