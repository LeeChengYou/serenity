# Serenity Signal V3 — 信任與時間適應需求規格

> 版本：v1.0 | 建立：2026-07-05 | 整理者：Fable（監督者）
> 依據：商業平台盲點掃描（Zacks/TipRanks/Danelfin/Seeking Alpha 對照）
> 本輪主題：**建立可驗證的信任 + 讓系統知道現在的市場情境**

---

## R3-1 命中率儀表板（P0，對應 roadmap B-1）

所有成熟商業平台的信任基石：公開展示「系統過去說了什麼 vs 實際發生什麼」。

**資料來源雙軌（誠實標記，不可混淆）：**
1. **實時記錄**：`signal_history` 每日快照（2026-07-04 起累積，目前僅數日）
2. **回溯重建**：用既有 `evaluate_symbol_at_cutoff` 零前瞻紀律，對過去每個
   快照日重建當日訊號 + 30 天後真實結果。前端必須明確標示
   「回溯重建（樣本外方法，但非實時發布）」vs「實時發布紀錄」。

**API 契約（BINDING）：**
```
GET /api/hitrate → {
  "as_of", "live_since",                    # 實時記錄起始日
  "summary": [{"signal", "n", "median_fwd_return_30d", "win_rate",
               "vs_universe", "source": "live|reconstructed"}],
  "recent_calls": [{"symbol","date","signal","close_then","close_now",
                    "fwd_return","universe_return","hit","source"}]  # 最近50筆
}
```
- `hit` 定義：BUY 類訊號 fwd_return > universe；EXIT 類 fwd_return < universe；
  HOLD/NEUTRAL 不計 hit。30 天未到期的標 `"hit": null`（pending）。
- 沿用全案鐵律：樣本不足標 insufficient，絕不硬給結論。

**前端**：新分頁「📈 命中率追蹤」——訊號別彙總表 + 最近呼叫明細
（成功/失敗/待定三色），頂部固定顯示 reliability 說明。

## R3-2 市場情境濾網 Regime Gauge（P0）

自家回測已證明 OVERBOUGHT 優勢只在多頭成立 → 系統必須知道現在的環境。

**資料**：新增基準 ETF 日線（SPY、SOXX、QQQ）——yfinance 下載，
存入既有 `prices` 表（symbol 照存，UI 的股票清單須排除基準符號）。

**Regime 計算（規則透明，不用 AI）：**
```
bull:    SPY>EMA200 且 SOXX>EMA200 且 universe>50% 站上 EMA50
bear:    SPY<EMA200 或 SOXX<EMA200*0.97
neutral: 其餘
```

**API 契約（BINDING）：**
```
GET /api/regime → {"as_of","regime":"bull|neutral|bear",
  "spy":{"close","ema200","above"}, "soxx":{...}, "qqq":{...},
  "universe_above_ema50_pct", "note"}   # note: 情境對訊號解讀的影響（zh-TW）
```

**整合**：
- dossier 的 Gemini prompt 加入 regime；空頭時 AI 建議必須降信心、
  OVERBOUGHT 類動能理由必須加警語
- 前端 header 常駐 regime 徽章（🟢多頭/🟡中性/🔴空頭）

## R3-3 分析師預估修正 + 評級（P1，Zacks 核心機制）

**資料**：yfinance（免費）——EPS 預估、預估修正方向、分析師評級分佈。
`python scripts/ingest.py estimates`，每週跑（J-7）。

**表**：`analyst_estimates(symbol pk, next_q_eps_avg, cur_y_eps_avg,
next_y_eps_avg, eps_revisions_up_30d, eps_revisions_down_30d,
rec_mean, rec_label, analyst_count, target_mean, target_vs_price, updated_at)`
缺欄位一律 NULL，絕不造假。

**API 契約（BINDING）：**
```
GET /api/estimates/<SYM> → 上表全欄位 + "revision_direction":"up|down|flat|null"
```
（revision_direction：up = 30 天內上修多於下修）

**前端**：基本面卡片旁新增「分析師預估」卡：評級均值、目標價 vs 現價、
修正方向箭頭。dossier prompt 同步納入。

## R3-4 財報行事曆警示（P1，資料已在庫）

- `fundamentals.next_earnings_date` 已有 → 前端在股票標頭顯示
  「📅 N 天後財報」徽章（≤7 天橙色、≤2 天紅色）
- 訊號面板：財報 ≤5 天時加警語「財報臨近，波動風險升高」
- dossier prompt 納入財報日期

## R3-5 訊號變化追蹤（P1，Danelfin 每日 delta 模式）

- 快照時比對前一日：訊號改變（如 HOLD→EXIT_ALERT）寫入
  `signal_changes(symbol, date, prev_signal, new_signal)`（冪等）
- `GET /api/changes?days=7` → 最近變化清單
- 前端側欄股票清單：24h 內有變化的股票加 Δ 徽章；
  新分頁或面板列出「最近 7 天訊號變化」

## 分工與邊界

| 工程師 | 範圍 | 檔案邊界 |
|--------|------|---------|
| E（後端） | R3-1 API+重建、R3-2 全部後端、R3-3 ingest+API、R3-5 後端 | scripts/*、docs/ROADMAP.md（J-7）；**不碰 dashboard/** |
| F（前端） | R3-1 前端頁、R3-2 徽章、R3-3 卡片、R3-4 全部、R3-5 前端 | dashboard/* only；**不碰 scripts/**，按上方 API 契約防禦性開發 |

## 驗收原則（沿用全案標準）

1. 零捏造——缺資料 NULL/insufficient；回溯 vs 實時必須標示
2. 零前瞻——重建沿用 evaluate_symbol_at_cutoff 紀律
3. 冪等、None 安全、優雅降級（基準 ETF 沒抓到時 regime 回 "unknown" 不擋版）
4. AI 建議在 bear regime 必須反映在措辭與信心度
