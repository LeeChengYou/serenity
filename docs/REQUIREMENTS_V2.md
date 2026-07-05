# Serenity Signal V2 — 全面投資決策支援需求規格

> 版本：v1.0 | 建立：2026-07-05 | 整理者：Fable（監督者）
> 來源：使用者需求「能夠全面給出購買建議的股票網站」

---

## 一、買賣決策所需數據盤點（R2-1）

一個負責任的買/賣建議需要六個面向的證據。現況盤點：

| 面向 | 內容 | 現況 | 缺口 |
|------|------|------|------|
| 技術面 | OHLC、EMA20/50、RSI、MACD、布林帶、ATR、量比 | ✅ 已有 | 前端呈現不足（見 R2-5） |
| 基本面 | P/E、forward P/E、EPS、營收成長、毛利率、市值、下次財報日 | ❌ 全缺 | **本輪新增**（Yahoo quoteSummary，免費） |
| 消息面 | 國際新聞、地緣政治、領導人/央行發言 | ❌ 全缺 | **本輪新增**（RSS，免費） |
| 情緒面 | X 貼文語料、StockTwits 群眾情緒 | ✅ 已有 | X cookie 待更新（A-3） |
| 相對強弱 | vs SOXX/SMH 基準 | ❌ 缺 | 後續輪次（C-4） |
| 訊號驗證 | 多窗口樣本外回測、成本敏感度 | ✅ 已有 | 持續累積樣本 |

## 二、每日更新排程（R2-2）

股票為時間序列資料，以下排程**必須每日執行**（J-1~J-3 已定義於 ROADMAP.md）：

| 任務 | 指令 | 頻率 |
|------|------|------|
| J-1 價格增量 | `python scripts/ingest.py prices` | 每日 06:30 |
| J-2 訊號快照 | `python scripts/server.py --snapshot-once` | 每日 06:40 |
| J-3 StockTwits | `python scripts/ingest.py stocktwits` | 每日 06:50 |
| **J-5 新聞抓取（新）** | `python scripts/ingest.py news` | 每日 07:00（可日內多次） |
| **J-6 基本面更新（新）** | `python scripts/ingest.py fundamentals` | 每週一 07:10 |

## 三、新聞管線（R2-3）

**來源（全免費、無需 API key）：**
- Google News RSS（`https://news.google.com/rss/search?q=<query>`）— 個股新聞主來源
- CNN RSS（`http://rss.cnn.com/rss/money_latest.rss` 等）— 國際/總經
- CNBC / Reuters Business RSS — 市場宏觀、領導人發言

**資料模型：** `news(id, title, source, url, published_at, scope, symbols, summary, fetched_at)`
- `scope`: `symbol`（個股）| `macro`（總經/地緣政治）
- 個股映射：symbol + 公司名關鍵詞查詢；macro 用固定查詢組（Fed、tariff、China chips、export controls…）
- 冪等：url 唯一鍵，重抓不重複

**API 契約（兩位工程師共同遵守）：**
```
GET /api/news/<SYM>   → {"symbol", "items":[{title,source,url,published_at,scope,summary}], "macro":[...同構...], "as_of"}
GET /api/fundamentals/<SYM> → {"symbol","pe","forward_pe","eps_ttm","revenue_growth_yoy",
                               "gross_margin","market_cap","next_earnings_date","updated_at"}
```
欄位缺資料一律回 `null`，前端須優雅處理。

## 四、AI 綜合買賣建議（R2-4）

擴充 `/api/dossier/<SYM>` 的 manager_view 輸入：
- 現有：量化分數、技術指標、訊號、情緒、評分卡
- **新增**：基本面摘要 + 近 7 天個股新聞標題 + 近 3 天 macro 新聞標題

輸出維持既有結構（thesis / bull_case / bear_case / conviction / recommendation），
recommendation 枚舉不變（AVOID|WATCH|ACCUMULATE|HOLD|REDUCE）。

**鐵律**：AI 只能引用 payload 內真實數據；`reliability_note` 誠實聲明必須保留；
Gemini 失敗時優雅降級（純數據 dossier）。

## 五、前端改版（R2-5）

**版面問題（使用者回饋）：**
1. 中心股價圖太小 → 主圖表為版面視覺中心，放大至主欄 60%+ 高度
2. 底部 X 貼文佔版過多 → 收進摺疊區/分頁，預設收合
3. 需要動態圖表 → 指標可即時切換疊加

**規格：**
- 主圖表：蠟燭圖（OHLC 已齊），指標開關列（EMA20/EMA50/布林帶/成交量），
  RSI/MACD 子圖可切換，時間範圍切換（1M/3M/6M/1Y/ALL），滑鼠十字線+tooltip
- 圖表庫：既有 Chart.js 可續用（含 chartjs-chart-financial 蠟燭圖插件，CDN 引入）；
  若確有必要可換 lightweight-charts，但須整體一致、不留兩套
- 新增面板：基本面卡片（P/E、營收成長…）、新聞列表（個股+macro 分區）
- X 貼文區：改為「證據」摺疊分頁，預設收起，不佔首屏
- 所有新面板對應 API 缺資料時顯示「資料未抓取」而非壞版

## 六、驗收原則（沿用全案標準）

1. 零捏造數據；新聞/基本面欄位缺就是 null，不填假值
2. 冪等抓取、None 安全、API 失敗優雅降級
3. AI 建議必附 reliability_note，不得宣稱未驗證的預測力
4. 前端在任一資料源缺失時仍可用（漸進增強）
