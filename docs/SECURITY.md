# Serenity Signal — 資訊安全評估與改善計畫

> 版本：v1.0 ｜ 建立：2026-07-07 ｜ 評估者：Claude（監督者，唯讀靜態分析）
> 範圍：production 本機的 server 架構（`scripts/server.py`）與程式架構。
> **評估方法：全程唯讀。未執行任何寫入、未觸碰線上資料庫內容、未讀取任何用戶隱私資料值
> （僅檢查 `.env` 的變數「名稱」，未讀取金鑰值；未讀取 `chat_monitor.json` 內容）。**

---

## 0. 一頁摘要（給決策者）

這是一個 Python 標準函式庫寫的單機 HTTP 服務（`ThreadingHTTPServer` +
`SimpleHTTPRequestHandler`），對外提供儀表板靜態檔與 JSON API，後端接 SQLite 與
Google Gemini API。**程式碼層級的基本功做得不錯**：SQL 全面參數化、前端有 `escapeHtml`、
機密走 `.env` 且已正確 gitignore、git 歷史無金鑰外洩、錯誤訊息有做（雖不完整的）金鑰遮罩、
POST 有 2MB 上限。

**但它的威脅模型是「只在 localhost 給自己用」，一旦當成 production 線上服務對外開放，
就有數個高風險缺口**，核心是三件事：

1. **完全沒有身分驗證** — 任何能連到這個 port 的人都能：讀取全部用戶聊天紀錄
   （`/api/monitor` 回傳最近 100 筆 prompt+response）、讀取用戶記憶（`/api/memory`）、
   清空用戶記憶（`POST /api/memory/clear`）、以及**無限制觸發 Gemini 呼叫燒光你的
   API 額度/帳單**（`/api/chat`、`/api/translate`、`/api/scorecard/generate`）。
2. **沒有 TLS** — 純 HTTP 明文，聊天內容與所有流量可被中間人竊聽。
3. **綁定位址與部署姿態未知** — 預設綁 `127.0.0.1`（安全），但 `--host` 可設 `0.0.0.0`。
   既然稱為「線上服務」，必須確認它是**躲在有 TLS+驗證的反向代理後面**，
   而不是直接把 `0.0.0.0:8787` 曝露到公網。

**最優先三件事**（詳見第 5 節）：① 確認/建立反向代理 + TLS + 存取驗證；
② 對所有會呼叫 Gemini 的端點加驗證與速率限制（防帳單攻擊）；③ 把 `/api/monitor`、
`/api/memory`、`/api/memory/clear` 這類敏感端點鎖起來或移除。

風險評級的**前提**：若此服務確實只綁 `127.0.0.1` 且機器本身無其他用戶、無公網轉發，
則多數「高」風險降為「低」——真正的風險完全取決於「它對誰可見」。第 1 節請務必先確認。

---

## 1. 部署事實（2026-07-07 已由使用者確認）

**現況**：目前**只在 localhost 執行**，沒有反向代理、沒有防火牆/資安設定、單人使用。
→ 因此第 4 節多數「🔴 高」風險**目前並未實際曝露**（沒有非信任方連得到），現階段風險為低。

**未來意圖**（使用者陳述）：可能 ① 開源到 GitHub 讓別人自訂、和/或 ② 部署到網路讓別人用。
→ 這兩條路線的第一風險完全不同，改善順序見**第 7 節**。**在走第 7 節之前，維持只綁
`127.0.0.1`、不要對外曝露**（等於還沒做 P1 就別開門）。

下表保留為「哪些條件會把風險從低變高」的對照：

| # | 問題 | 若答案是… | 風險 |
|---|------|-----------|------|
| Q1 | server 啟動時 `--host` 是什麼？ | `0.0.0.0` 或空白對公網 | 🔴 所有無驗證端點全部曝露 |
| Q2 | 前面有沒有 nginx/Caddy 之類反向代理？ | 沒有 | 🔴 無 TLS、無存取控制 |
| Q3 | 這台機器除了你還有誰能登入/同網段？ | 有他人 | 🟠 localhost 綁定也擋不住同機用戶 |
| Q4 | port 8787 有沒有被防火牆對外放行？ | 有 | 🔴 等同公開 |
| Q5 | 有沒有網域/DNS 指到這台機器？ | 有 | 🔴 確定是對外服務 |

> 本文件其餘部分**假設最壞情況**（服務對非信任網路可見）來評估，這樣的改善計畫在
> 「其實只有 localhost」的情況下也只是多做了不會出錯的縱深防禦。

---

## 2. 架構快照（評估對象）

**Server**（`scripts/server.py`，約 3000+ 行）
- `ThreadingHTTPServer` + 自訂 `Handler(SimpleHTTPRequestHandler)`；每連線一執行緒。
- 綁定：`--host` 預設 `127.0.0.1`、`--port` 預設 `8787`（`server.py:3030,3049`）。
- 靜態檔：`directory=dashboard/`（`server.py:440`）。
- 資料庫：`data/serenity.sqlite`，WAL 模式（`server.py:72,276`）。
- AI：Google Gemini `generativelanguage.googleapis.com`，`urllib` 直呼，60s timeout，
  `KeyManager` 管 4 把金鑰做 429 failover（`server.py:235-270`）。
- 機密：`.env`（`GEMINI_API_KEY`~`_4`、`GEMINI_MODEL`），已 gitignore、未進 git 歷史（已驗證）。

**API 端點盤點**
- GET（唯讀查詢）：`/api/config`、`/api/monitor`、`/api/keypool`、`/api/regime`、
  `/api/hitrate`、`/api/changes`、`/api/summary`、`/api/feed`、`/api/symbol/*`、
  `/api/signal/*`、`/api/news/*`、`/api/estimates/*`、`/api/fundamentals/*`、
  `/api/dossier/*`、`/api/scorecard/*`、`/api/memory`、`/api/expert-views*`、`/api/arena/*`。
- POST（有副作用/呼叫 AI）：`/api/chat`、`/api/translate`、`/api/memory/clear`、
  `/api/scorecard/generate/*`。

**前端**：純 vanilla JS（`dashboard/app.js`），`fetch` 打上述 API，`innerHTML` 渲染，
有 `escapeHtml` 輔助函式。

---

## 3. 威脅模型

**資產**：① Gemini API 金鑰與其計費額度；② 用戶聊天紀錄/記憶（隱私）；③ SQLite 資料
完整性；④ 服務可用性；⑤ X session cookies（`x_curl/`、`data/browser_profile/`）。

**攻擊者**（依 Q1–Q5 而定）：公網掃描者、同網段用戶、同機其他帳號、惡意網頁
（透過受害者瀏覽器對 localhost 發 CSRF 式請求）、供應鏈（Gemini 回應被當輸入）。

**信任邊界**：瀏覽器 ↔ HTTP 服務、HTTP 服務 ↔ SQLite、HTTP 服務 ↔ Gemini。
目前**第一條邊界完全不設防**（無驗證、無 TLS）。

---

## 4. 發現清單（按風險排序）

> 嚴重度前提見第 0、1 節：多數「高」的成立條件是「服務對非信任網路可見」。
> 每條附：證據（檔案:行號）、影響、修法。

### 🔴 F-1　所有端點零身分驗證
- **證據**：`Handler.do_GET`/`do_POST`（`server.py:442,460`）直接路由，無任何 token/session 檢查。
- **影響**：服務一旦可被非信任方連到，下列全部無門檻可用——見 F-2~F-5。這是所有其他
  高風險的根。
- **修法**：見改善計畫 P1-1（反向代理 + 前置驗證）與 P1-2（應用層共享密鑰）。

### 🔴 F-2　無驗證的 AI 端點 = 帳單/額度耗盡攻擊
- **證據**：`POST /api/chat`（`server.py:619`）、`/api/translate`（`622`）、
  `/api/scorecard/generate/*`（`634`）都會呼叫 `call_gemini`，無速率限制、無驗證。
- **影響**：攻擊者（或惡意網頁透過受害瀏覽器）可迴圈打這些端點，燒光 4 把金鑰的免費/付費
  額度，造成停用或帳單暴增。這是**最可能被自動化濫用**的一條。
- **修法**：P1-2 驗證 + P2-1 速率限制（每 IP/每密鑰的 QPS 與日上限）+ P2-4 全域每日
  Gemini 呼叫預算熔斷。

### 🔴 F-3　`/api/monitor` 無驗證洩漏全部聊天紀錄
- **證據**：`server.py:500-508` 讀 `data/chat_monitor.json` 直接回傳；該檔由
  `log_chat_transaction`（`814-828`）寫入，內容含 `prompt`（用戶原話）、`response`
  （AI 回覆）最近 100 筆（`997-1004`）。
- **影響**：任何能連到服務者可一次撈走所有歷史對話——直接的隱私外洩。
- **修法**：P1-3 立即把此端點納入驗證或直接移除前端不需要的除錯端點；`chat_monitor.json`
  屬用戶隱私，考慮縮短保留、去識別化或加密靜態儲存。

### 🔴 F-4　無 TLS（明文 HTTP）
- **證據**：`send_json`/`SimpleHTTPRequestHandler` 皆純 HTTP，無 TLS 設定。
- **影響**：聊天內容、API 回應在網路上明文傳輸，可被竊聽/竄改。
- **修法**：P1-1 由反向代理終結 TLS（Let's Encrypt）。**不要**在 Python stdlib 這層自簽硬做。

### 🟠 F-5　無驗證的狀態變更端點
- **證據**：`POST /api/memory/clear`（`server.py:624-631`）可清空 `user_memories`
  整張表；`GET /api/memory`（`587-592`）回傳全部記憶。
- **影響**：未授權的資料破壞（清記憶）與隱私讀取。CSRF 亦可觸發（無 CSRF token、無
  SameSite 保護，因為根本沒有 session）。
- **修法**：P1-2 驗證 + P2-2 對所有 POST 加 CSRF 對策（或要求自訂 header + 同源檢查）。

### 🟠 F-6　連線洪泛 DoS（每連線一執行緒，無上限）
- **證據**：`ThreadingHTTPServer`（`server.py:15,3049`）為每個連線開執行緒，無並發上限、
  無連線速率限制。
- **影響**：大量連線即可耗盡執行緒/記憶體使服務癱瘓。
- **修法**：P2-1 由反向代理做連線數/速率限制；應用層可加簡單信號量上限。

### 🟠 F-7　錯誤訊息可能洩漏內部資訊（金鑰遮罩是脆弱的黑名單）
- **證據**：例外處理把 `str(exc)` 直接回給客戶端（`server.py:451-454,477-480,807-810`），
  只在字串含 `key=`/`api_key`/`goog-api-key` 時才遮罩。
- **影響**：其他例外（SQL 錯誤、檔案路徑、堆疊細節）會原文回傳，洩漏內部結構供攻擊者利用；
  黑名單式遮罩漏掉任何未列的金鑰格式就破功。
- **修法**：P2-3 對外一律回制式訊息（如「內部錯誤，請稍後再試」+ 一個關聯 ID），
  詳細錯誤只寫伺服器端日誌（`traceback.print_exc()` 已這麼做，保留）。

### 🟡 F-8　前端 XSS 表面：`innerHTML` 廣泛使用（需完整逐一稽核）
- **證據**：`dashboard/app.js` 有 42 處 `innerHTML`。抽樣檢查顯示**用戶可見的不可信內容
  （貼文文字、新聞、evidence、13F 作者）多數有經 `escapeHtml`**（如 `app.js:112,113,
  537,725,728,730`）——這是好的。
- **影響**：風險取決於是否有**任一** `innerHTML` sink 塞入未跳脫的不可信資料（X 貼文、
  新聞標題、AI 產出、記憶內容）。因服務無 cookie/session，XSS 無法竊取憑證，但仍可用於
  頁面竄改或竊取當前頁面上顯示的資料。
- **修法**：P2-5 對全部 42 處 sink 做一次逐一稽核，確認每個插值都經 `escapeHtml`，
  或改用 `textContent`/建 DOM 節點；把「不可信資料一律跳脫」寫進前端規範。

### 🟢 F-9　SQL Injection：目前未發現可利用點（正面）
- **證據**：server 端所有含使用者輸入的查詢都用參數綁定（`?`），symbol 路徑參數雖
  `.upper()` 仍以綁定傳入（如 `server.py:544,570,575,639`）。`agent_arena.py` 的
  `.format()` 只用來生成 `in (?,?,?)` 佔位符（`agent_arena.py:463,507,1052,1117`），
  `ingest.py`/`fetch_tv_price.py` 的 `ALTER TABLE ADD {col}` 之 `col` 來自內部白名單、
  非使用者輸入。
- **結論**：維持現狀即可；新增查詢時**繼續只用參數綁定，永不字串拼接使用者輸入**。

### 🟢 F-10　機密管理：目前良好（正面，維持）
- **證據**：`.env` 已 gitignore（`.gitignore` 有 `.env`/`.env.*`）、未被 git 追蹤、
  git 歷史掃描無 `AIza` 金鑰外洩；`/api/keypool` 只回金鑰後綴不回全值
  （`server.py:510-514`）；錯誤訊息有金鑰遮罩嘗試。
- **結論**：維持。改善點併入 F-7（遮罩改白名單式輸出）與 P3-2（金鑰輪替流程文件化）。

### 🟢 F-11　路徑穿越：低風險（依賴 Python 版本）
- **證據**：靜態檔由 `SimpleHTTPRequestHandler` 服務，現代 Python 會正規化 `..`。
- **修法**：P3-1 於改善時記錄最低 Python 版本要求，避免降版引入舊版穿越漏洞。

### ⚪ F-12　Prompt Injection（LLM 特有，影響有限但要知道）
- **證據**：用戶 `/api/chat`、`/api/translate` 文字與 DB 貼文內容會拼進 Gemini 的
  system/user prompt（`server.py:686,975+`）。
- **影響**：使用者可誘導 AI 產出被操縱的文字回應。因為 server 端**不會**把這個聊天/翻譯
  的 LLM 輸出回灌成系統動作（competition arena 的 JSON 解析是獨立的內部流程，已在
  `LESSONS.md` 記錄畸形→拒單防護），所以爆炸半徑限於「AI 講了被操縱的話」，不致命。
- **修法**：P3-3 保持「LLM 輸出永遠是不可信資料」原則；任何未來把聊天 AI 輸出接進
  自動動作的設計，必須先過白名單驗證。

---

## 5. 改善計畫（分階段、可執行）

> 排序原則：先關「對外曝露 + 會花錢/洩隱私」的洞，再做縱深防禦，最後是流程與硬化。
> 每項標註〔對應發現〕與驗收方式。**所有變更都應在非線上環境先驗證，不改動線上資料。**

### Phase P1 — 止血（對外曝露前必做，1–2 天）

- **P1-1　前置反向代理 + TLS + 存取控制**〔F-1,F-4,F-6〕
  在 server 前面擺 nginx 或 Caddy：終結 HTTPS（Let's Encrypt 自動憑證）、只反代到
  `127.0.0.1:8787`、加上最基本的存取驗證（見 P1-2）。**server 本身維持只綁
  `127.0.0.1`，永不直接 `--host 0.0.0.0` 對公網。**
  驗收：外部 `curl http://<host>:8787` 連不到；`https://<域名>` 正常且憑證有效。

- **P1-2　存取驗證**〔F-1,F-2,F-5〕
  最低成本：反向代理層 HTTP Basic Auth 或一個長隨機 Bearer token，前端帶在 header。
  若要多用戶，才上正式的登入。**先求有門，再求好門。**
  驗收：不帶憑證打任一 `/api/*` 回 401；帶正確憑證才通。

- **P1-3　鎖定/移除敏感端點**〔F-3,F-5〕
  `/api/monitor`（除錯用）在 production 直接關閉或強驗證；`/api/memory`、
  `/api/memory/clear` 納入驗證。確認 `chat_monitor.json` 不被任何無驗證路徑讀到。
  驗收：未授權存取上述端點回 401/404，且回應不含任何 prompt/response 內容。

### Phase P2 — 縱深防禦（本週）

- **P2-1　速率限制**〔F-2,F-6〕反向代理層對 `/api/*`（尤其 AI 端點）設每 IP QPS 與
  突發上限；連線數上限。驗收：超速請求回 429。
- **P2-2　CSRF 對策**〔F-5〕所有 POST 要求自訂 header（如 `X-Requested-With`）+ 同源
  `Origin`/`Referer` 檢查，拒絕跨站表單式請求。驗收：偽造跨站 POST 被拒。
- **P2-3　統一錯誤回應**〔F-7〕對外一律回制式訊息 + 關聯 ID，細節只進伺服器日誌；
  移除對客戶端回 `str(exc)`。驗收：故意觸發 SQL/檔案錯誤，客戶端看不到內部細節。
- **P2-4　Gemini 全域預算熔斷**〔F-2〕在 `call_gemini` 或 `KeyManager` 加「當日累計呼叫
  達 N 次即拒絕並告警」，防止額度被一次打爆。驗收：模擬超量後端點回制式「服務忙碌」。
- **P2-5　前端 XSS 逐一稽核**〔F-8〕清點全部 42 處 `innerHTML`，確認每個不可信插值都經
  `escapeHtml` 或改 `textContent`。驗收：以含 `<img onerror>` 的測試貼文/新聞資料
  （放本機測試 DB，不動線上）確認不執行。

### Phase P3 — 硬化與流程（本月）

- **P3-1　部署硬化文件**：以非 root 帳號跑、systemd 沙箱（`ProtectSystem`、
  `NoNewPrivileges`、`PrivateTmp`）、記錄最低 Python 版本〔F-11〕。
- **P3-2　金鑰輪替流程**〔F-10〕：把「如何換 4 把 Gemini 金鑰、疑洩漏時如何撤銷」寫成
  runbook；確認金鑰不進任何日誌。
- **P3-3　安全不變式寫進制度**：把「LLM 輸出=不可信」〔F-12〕、「使用者輸入只用參數綁定」
  〔F-9〕、「不可信資料一律跳脫」〔F-8〕加進 `docs/agents/` 的品質底線與審查模板。
- **P3-4　依賴與備份**：`data/serenity.sqlite` 定期 `PRAGMA integrity_check` +
  離線備份（OneDrive 同步下有損毀風險，見 `docs/agents/LETTER.md`）。

### 明確不建議做的事
- 不要在 Python stdlib server 這層自己實作 TLS/驗證/速率限制——交給成熟的反向代理，
  應用層只做業務邏輯。
- 不要為了「快」直接 `--host 0.0.0.0` 曝露；那等於跳過整個 P1。

---

## 7. 開源與部署路線（2026-07-07 新增）

你講的兩個未來方向是**兩種不同的工程**，第一風險不同，請分開處理、不要混做。

### 先釐清一個關鍵分岔：「別人自訂」是哪一種？

- **模式 S（自架 self-host）**：別人 clone 你的 repo，用**他自己的機器、他自己的 Gemini
  金鑰、他自己的資料庫**跑一份。你只發程式碼，不幫任何人代管。
- **模式 M（多租戶代管 multi-tenant SaaS）**：你架一個網站，很多人**同時連你這台、共用
  你的後端**。

**強烈建議走模式 S。** 理由：現在的架構是**單租戶**設計——`user_memories` 是全域一張表、
`chat_monitor.json` 是全域一份、Gemini 金鑰是你的。模式 M 需要把這些全部改成「每個使用者
隔離」＋帳號系統＋「自帶金鑰或你替所有人付錢」，這是**重寫等級**的工程，且 F-2 帳單攻擊
會直接落到你頭上。模式 S 幾乎不用改架構（每個人跑自己的），還完全符合本專案 local-first
的定位。**開源（模式 S）和多租戶上線（模式 M）可以先做前者，後者當獨立的大專案另議。**

### 路線 A — 開源到 GitHub（模式 S，低成本，可近期做）

第一風險是**把機密/隱私推進公開 repo**，以及**別人 naive 部署就繼承你所有無驗證漏洞**。
開源前置檢查清單（Pre-publish checklist）：

- [ ] **A-1 機密掃描**〔已部分驗證〕：`.env` 已 gitignore、git 歷史無 `AIza` 金鑰（已確認）。
  發佈前再跑一次全歷史掃描（如 `gitleaks detect` 或 `git log -S`），確認 cookies/token
  從未被 commit。
- [ ] **A-2 清掉追蹤中的資料檔**：目前 git 追蹤了 `data/tsm_scorecard.json`（生成的個股
  分析，非機密但屬雜訊）與 `dashboard/monitor.html`（除錯監控頁，指向會洩漏聊天紀錄的
  `/api/monitor`）。發佈前決定：是否移出版控（`git rm --cached`）或保留。**確認
  `data/serenity.sqlite`、`data/chat_monitor.json` 未被追蹤**（已 gitignore，需再確認
  無歷史殘留）。
- [ ] **A-3 加 `LICENSE`**〔目前缺〕：**沒有 LICENSE 的公開 repo 在法律上不是開源**，
  別人不能合法使用。選一個授權（MIT/Apache-2.0 最常見）。注意：本 repo 是 haskaomni/serenity
  的 fork，公開前確認上游授權允許你這樣散布。
- [ ] **A-4 加 `.env.example`**〔目前缺〕：列出 `GEMINI_API_KEY`~`_4`、`GEMINI_MODEL`
  等變數名稱（**只有名稱、值留空**），讓別人知道要設什麼。
- [ ] **A-5 README 安全警告**：明寫「本服務預設無驗證、無 TLS，僅供 localhost 使用；
  未加反向代理+驗證+TLS 前，切勿 `--host 0.0.0.0` 對外曝露」。把第 4 節 F-1~F-5 濃縮成
  一段警語。
- [ ] **A-6 根目錄 `SECURITY.md`（回報政策）**：GitHub 慣例——一頁說明「發現漏洞如何回報」。
  （本檔 `docs/SECURITY.md` 是內部評估，用途不同，兩者可並存。）
- [ ] **A-7 移除個人資訊**：確認 commit author、README、程式碼註解無真實 email、
  本機絕對路徑（如 `C:\Users\Jeff\...`）等個資。
- [ ] **A-8 fork 關係**：確認 GitHub 上要公開的是你想公開的內容，且不會誤觸「向上游發 PR」
  （見 `docs/agents/` 工作規範：只推 origin）。

### 路線 B — 部署到網路多人使用（模式 M，大工程，獨立專案）

**前置條件：路線 A 的 A-1~A-2 + 第 5 節 P1（反向代理+TLS+驗證）全部完成。** 之外還需要
本專案目前完全沒有的東西：

- **B-1 帳號與驗證系統**：真正的多使用者登入（非單一共享密碼）。
- **B-2 每使用者資料隔離**：`user_memories`、聊天紀錄、（若有）持倉都要綁 user_id；
  現在是全域共用，多人上線會互相看到彼此資料＝隱私事故。
- **B-3 金鑰模型**：要嘛「每個使用者自帶 Gemini 金鑰」（推薦，成本與濫用都轉嫁給用戶），
  要嘛你替所有人付費（則 F-2 帳單攻擊 + P2-1 速率限制 + P2-4 預算熔斷變成生死問題）。
- **B-4 合規**：一旦替他人儲存個資，可能涉及隱私法規（同意書、資料刪除權、保存期限）。
- **B-5 完整跑完第 5 節 P1→P2→P3。**

**建議**：模式 M 不要在現有 code base 上「加一加」，那會做出一個帳號和資料隔離都半吊子的
危險系統。當成一個明確的新里程碑（甚至新的後端框架）重新設計。

---

## 6. 本次評估的限制（誠實條款）

- **未做動態測試**：全程唯讀靜態分析，未實際發送攻擊請求、未壓測、未跑 DAST/滲透工具。
  上述風險是從程式碼推導，實際可利用性需在**隔離測試環境**（複製一份、不連線上資料）驗證。
- **部署姿態未知**：真正的風險等級取決於第 1 節 Q1–Q5，程式碼看不出來，需你確認。
- **未讀取任何隱私資料值**：未開啟 `chat_monitor.json`、未讀 `.env` 金鑰值、未查詢線上
  資料庫內容——因此無法評估「已累積多少隱私資料」，只能指出資料流路徑。
- **第三方**：未評估 Gemini API 端、OneDrive 同步層、作業系統層的安全，僅限本 repo 程式碼。
- **XSS 為抽樣結論**：F-8 抽樣顯示 `escapeHtml` 有被使用，但未逐一驗證全部 42 處；
  P2-5 才是完整結論。
