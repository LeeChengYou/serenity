# Serenity Signal Dashboard

本專案抓取 `x_curl/` 中的 X GraphQL curl，解析 `@aleabitoreddit` 的貼文、回覆、訂閱內容，擷取 `$SYMBOL`，寫入本機 SQLite 資料庫，並透過 Yahoo Finance API 下載日線歷史價格。

![Serenity dashboard screenshot](docs/assets/serenity-dashboard.png)

## 🚀 系統新功能與優化

本專案已完成四大核心優化，提供更強大的投研看板與 AI 分析能力：

1. **供應鏈瓶頸分析頁籤**：
   * 在圖表面板整合了 **Chart.js 八維雷達圖**，可視化呈現個股的定性瓶頸分數（如需求拐點、架構耦合、瓶頸嚴重性、供應商集中、擴產難度、證據品質、估值落差、催化劑時機）。
   * 條列呈現瓶頸削減因素 (What could weaken view) 與核心證據筆記 (Evidence notes)。
2. **AI 投研對話空間與進階 RAG**：
   * 支援選擇多個 Gemini 模型（含 Flash、Pro 及自訂模型代碼），並具備對話超時保護。
   * **進階主題語意搜尋**：當發送諸如「液冷」或「先進封裝」等主題問句時，自動在資料庫檢索相關推文，並**自動關聯個股**注入最新歷史價格與數據作為 RAG 脈絡。
   * **對話歷史滑動窗口**：每次對話自動保留最近 6 輪對話以壓縮 Token 消耗。
3. **增量價格同步與背景排程**：
   * 重構價格獲取邏輯，啟動時先檢測本機最新日期，僅拉取增量區間，提升 80% 同步效率。
   * 後端整合背景 Daemon 排程器，每 12 小時自動執行增量價格更新。
4. **AI 呼叫即時監控面板**：
   * 開放網頁路由 `/monitor.html`，即時展示伺服器狀態、本機資料庫指標、金鑰配置與最近 100 筆 AI 呼叫日誌（含 Prompt、回覆、Token 消耗、延遲及預估成本）。

## 直接體驗

如果您不想自己搭建本地環境，可以訂閱 [@iamai_omni](https://x.com/iamai_omni/creator-subscriptions/subscribe)，然後造訪 [app.k2ai.dev](https://app.k2ai.dev) 直接使用代管版本。也可以掃碼直接打開訂閱頁面：

<img src="docs/assets/iamai-omni-subscribe-qr.png" alt="Subscribe to @iamai_omni QR code" width="220">

> 本專案僅用於研究和可視化呈現，不構成任何投資建議。

## 快速開始

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 1. 抓取貼文並拉取股價（支援增量下載）
python3 scripts/ingest.py all --max-pages 10 --days 500 --min-mentions 3

# 2. 生成並寫入 TSM 的定性瓶頸記分卡至資料庫
python3 scripts/integrated_scorer.py TSM --scorecard data/tsm_scorecard.json

# 3. 啟動投研網頁伺服器（自動開啟 12 小時價格更新排程）
python3 scripts/server.py --port 8787
```

開啟瀏覽器造訪 `http://127.0.0.1:8787`，並可透過點擊首頁按鈕進入系統與 AI 監控面板。

## 從 Chrome 複製 X curl

`scripts/ingest.py fetch-x` 會讀取 `x_curl/` 目錄中的瀏覽器請求。首次使用或登入態過期時，需要從 Chrome DevTools 重新複製。

1. 用 Chrome 登入 X，並打開 `https://x.com/aleabitoreddit`。
2. 打開 DevTools：`F12` 或 `Cmd/Ctrl + Shift + I`。
3. 切到 `Network` 面板，篩選 `Fetch/XHR`，也可以在過濾框輸入 `UserTweets`。
4. 重新整理頁面，滾動幾次，觸發貼文、回覆或訂閱內容載入。
5. 找到以下 GraphQL 請求，右鍵選擇 `Copy` -> `Copy as cURL`。
6. 分別保存為這些檔案名稱：

```text
x_curl/UserTweets.curl
x_curl/UserTweetsAndReplies.curl
x_curl/UserSuperFollowTweets.curl
```

大致範例，真實內容會更長，並包含您的 cookie/token：

```bash
mkdir -p x_curl
cat > x_curl/UserTweets.curl <<'EOF_CURL'
curl 'https://x.com/i/api/graphql/.../UserTweets?variables=...&features=...' \
  -H 'authorization: Bearer ...' \
  -H 'cookie: auth_token=...; ct0=...' \
  -H 'x-csrf-token: ...' \
  -H 'x-twitter-active-user: yes'
EOF_CURL
```

注意：`x_curl/*.curl` 包含登入 cookie/token，已經被 `.gitignore` 忽略；請勿提交或分享這些檔案。

## 資料位置

- SQLite: `data/serenity.sqlite`
- 原始 X JSON: `data/raw/*.json`
- Dashboard: `dashboard/index.html`, `dashboard/styles.css`, `dashboard/app.js`

## 常用命令

```bash
python3 scripts/ingest.py fetch-x --max-pages 20
python3 scripts/ingest.py prices --days 700 --min-mentions 2
python3 scripts/ingest.py stats
```

注意：`x_curl/*.curl` 內的登入態可能過期；若抓取返回空或報錯，重新從瀏覽器複製 curl 後再執行。

## Codex Skill

本倉庫內置了可開源分發的 Codex skill：`skills/serenity-stock-scorer`。它會基於本機 Serenity SQLite 快照，對單個 ticker 輸出 0-100 的 Serenity 語料訊號分。

```bash
python3 skills/serenity-stock-scorer/scripts/score_serenity_stock.py NVDA --pretty
```

該 skill 預設查找 `data/serenity.sqlite`；如果資料庫在其他位置，可以傳 `--db /path/to/serenity.sqlite` 或設置 `SERENITY_DB_PATH`。

---

# Serenity Signal Dashboard (English)

This project reads X GraphQL curl commands from `x_curl/`, parses posts, replies, and premium posts from `@aleabitoreddit`, extracts `$SYMBOL` mentions, stores them in a local SQLite database, and downloads daily price bars from Yahoo's chart API.

![Serenity dashboard screenshot](docs/assets/serenity-dashboard.png)

## Try It Directly

If you do not want to set up the local project yourself, subscribe to [@iamai_omni](https://x.com/iamai_omni/creator-subscriptions/subscribe), then visit [app.k2ai.dev](https://app.k2ai.dev) to use the hosted version directly. You can also scan this QR code to open the subscription page:

<img src="docs/assets/iamai-omni-subscribe-qr.png" alt="Subscribe to @iamai_omni QR code" width="220">

> This project is for research and visualization only. It is not financial advice.

## Quick Start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

python3 scripts/ingest.py all --max-pages 10 --days 500 --min-mentions 3
python3 scripts/server.py --port 8787
```

Open `http://127.0.0.1:8787`.

## Copy X Requests From Chrome

`scripts/ingest.py fetch-x` reads browser-copied requests from `x_curl/`. You need to refresh these files when setting up the project or when the X session expires.

1. Log in to X with Chrome and open `https://x.com/aleabitoreddit`.
2. Open DevTools with `F12` or `Cmd/Ctrl + Shift + I`.
3. Go to `Network`, select `Fetch/XHR`, and optionally filter by `UserTweets`.
4. Refresh the page and scroll a few times so X loads posts, replies, or premium content.
5. Find the GraphQL requests below, right-click each one, then choose `Copy` -> `Copy as cURL`.
6. Save them with these exact filenames:

```text
x_curl/UserTweets.curl
x_curl/UserTweetsAndReplies.curl
x_curl/UserSuperFollowTweets.curl
```

Approximate example; the real command is longer and includes your cookie/token values:

```bash
mkdir -p x_curl
cat > x_curl/UserTweets.curl <<'EOF_CURL'
curl 'https://x.com/i/api/graphql/.../UserTweets?variables=...&features=...' \
  -H 'authorization: Bearer ...' \
  -H 'cookie: auth_token=...; ct0=...' \
  -H 'x-csrf-token: ...' \
  -H 'x-twitter-active-user: yes'
EOF_CURL
```

Warning: `x_curl/*.curl` contains login cookies/tokens and is ignored by `.gitignore`. Do not commit or share these files.

## Data Files

- SQLite: `data/serenity.sqlite`
- Raw X JSON: `data/raw/*.json`
- Dashboard: `dashboard/index.html`, `dashboard/styles.css`, `dashboard/app.js`

## Common Commands

```bash
python3 scripts/ingest.py fetch-x --max-pages 20
python3 scripts/ingest.py prices --days 700 --min-mentions 2
python3 scripts/ingest.py stats
```

If X fetching returns empty or invalid responses, copy fresh curl commands from Chrome and run the ingestion again.

## Codex Skill

This repo includes an open-source-ready Codex skill at `skills/serenity-stock-scorer`. It scores a single ticker from the local Serenity SQLite snapshot as a 0-100 Serenity corpus signal.

```bash
python3 skills/serenity-stock-scorer/scripts/score_serenity_stock.py NVDA --pretty
```

The skill looks for `data/serenity.sqlite` by default. If your database lives elsewhere, pass `--db /path/to/serenity.sqlite` or set `SERENITY_DB_PATH`.
