# REQUIREMENTS V7 — 多人上線（Multi-tenant Online）架構規格

> 版本：v1.0 ｜ 建立：2026-07-07 ｜ 監督：Claude
> 前置文件：`docs/SECURITY.md`（威脅模型與 P1–P3）、`docs/agents/`（工作規範）。
> 性質：**架構與遷移規格，不是實作**。實作按第 8 節分階段，由 Sonnet subagent 執行、
> 監督者逐階段驗收。本文件為各階段的綁定契約。

---

## 0. 使用者已定案的決策（2026-07-07）

| 決策 | 選擇 | 影響 |
|------|------|------|
| AI 金鑰/費用 | **使用者自帶金鑰（BYO）** | 無擁有者帳單風險；F-2 帳單攻擊消失；需加密儲存每人金鑰 |
| 使用者規模 | **小圈子 <50、信任的人** | 可用單一 Postgres 實例；邀請制起步；不需極端擴展設計 |
| 帳號登入 | **社群 OAuth（Google/GitHub）** | 不儲存密碼；個資最少；用 Authlib，不自幹帳密 |
| 技術棧 | **換 Web 框架 + 正式資料庫** | 建議 FastAPI + PostgreSQL + SQLAlchemy，保留現有 Python 分析模組 |

**非目標（本版明確不做）**：付費/計費系統、每使用者專屬競技場、行動 App、
水平擴展到數千人。這些留待未來版本。

---

## 1. 核心架構洞見：資料二分法

實作前必須內化這條，否則會過度工程：

### 全域公共資料（唯讀給使用者，由擁有者 pipeline 維護）
`prices`、`mentions`、`tweets`、`signal_history`、`scorecards`、`scorecard_history`、
`fundamentals`、`estimates`、`benchmarks`、`news`、專家觀點(13F)、
競技場全部（`agents`、`agent_*`）。
→ **每個使用者看到的內容相同**；不需 `user_id`；遷移時整表搬到 Postgres 即可。
→ 競技場是**全域展示**（大家看同一場 9-agent 競賽），不是每人一份。

### 每人私有資料（需 `user_id` 隔離 + 存取控制）
- `users`（新）：OAuth 身分、建立時間、狀態。
- `user_api_keys`（新）：每人的 BYO Gemini 金鑰，**加密儲存**（見 R7-4）。
- `user_memories`：現為全域一張表 → **加 `user_id` 欄位**，查詢一律帶 `where user_id=?`。
- `user_chat_log`（新，取代全域 `data/chat_monitor.json`）：每人聊天紀錄，綁 `user_id`。
- （未來）`user_watchlist`、`user_holdings`：預留，本版不做但 schema 設計要能容納。

**結論**：需要改動隔離邏輯的只有上面「每人私有」這一小組；全域資料只是換儲存後端。

---

## 2. 目標技術棧（建議，實作前可再議）

| 層 | 選擇 | 理由 |
|----|------|------|
| Web 框架 | **FastAPI** | async、Pydantic 輸入驗證、OAuth 生態成熟、可直接複用現有 Python 模組 |
| 資料庫 | **PostgreSQL** | 多人並發寫入安全（SQLite 的寫鎖在多人下會卡） |
| ORM/存取 | **SQLAlchemy 2.x**（+ Alembic 遷移） | 型別安全、遷移可版控 |
| 驗證 | **Authlib** 接 Google/GitHub OAuth | 不自幹帳密；不儲存密碼 |
| Session | 簽章 cookie 或短期 JWT（HttpOnly、Secure、SameSite=Lax） | 防 XSS 竊 session、防 CSRF |
| 前端 | **沿用現有 vanilla JS 儀表板**，改打新 API | 不需重寫前端；只換資料來源與加登入態 |
| 部署 | 單一小型 VPS 或 Railway/Render/Fly.io + 託管 Postgres | <50 人不需複雜編排；平台幫你處理 TLS |
| 機密 | 平台環境變數 / secrets manager | OAuth client secret、DB 連線字串、加密主金鑰 |

**保留不動**：`scripts/ingest.py`、`indicators.py`、`signals.py`、`integrated_scorer.py`、
`agent_arena.py`、`backtest_*.py`、`crawler.py`——這些是純 Python 分析/資料邏輯，
FastAPI 直接 import 呼叫即可。**心臟不換，只換血管與門禁。**

---

## 3. 需求條目

### R7-1　OAuth 登入與 users 表
- 支援 Google 與/或 GitHub OAuth 登入。首次登入自動建 `users` 列（存 provider、
  provider_user_id、email、display_name、created_at、status）。
- 登入後發 HttpOnly + Secure + SameSite=Lax 的 session cookie / JWT。
- **邀請制**：`users.status` 預設 `pending`，擁有者核准後才 `active`（<50 人、信任圈適用）；
  或用 email 白名單。未核准者可登入但看不到私有功能。
- 登出清除 session。

### R7-2　所有 API 端點的存取控制
- **公共唯讀端點**（價格/訊號/新聞/評分卡/競技場…）：登入後可讀；是否允許未登入唯讀由
  擁有者設定（預設要登入）。
- **私有端點**（聊天、記憶、金鑰設定）：**必須驗證，且只能存取自己的 `user_id` 資料**。
- 移除或永久鎖死 `/api/monitor`（原全域聊天紀錄洩漏端點，見 SECURITY F-3）。
- 對應 SECURITY.md F-1（零驗證）、F-3、F-5 的根本修復。

### R7-3　資料隔離（每一條私有查詢都綁 user_id）
- `user_memories`、`user_chat_log` 的每一次 select/insert/delete 都必須帶
  `where user_id = <當前登入者>`。**沒有任何路徑能讀到別人的私有資料。**
- 驗收測試必含「使用者 A 無法讀/改使用者 B 的記憶與聊天」的紅線案例。

### R7-4　BYO 金鑰儲存與使用
- 使用者在設定頁輸入自己的 Gemini 金鑰 → **加密後**存入 `user_api_keys`
  （用 app 層對稱加密，主金鑰放環境變數/secrets manager；DB 外洩也拿不到明文）。
- `call_gemini` 改為**用當前登入使用者的金鑰**呼叫；使用者沒設金鑰時，AI 功能顯示
  「請先到設定頁填入你的 Gemini 金鑰」，不 fallback 到擁有者金鑰。
- 金鑰值**永不**回傳前端（設定頁只顯示「已設定 ****後4碼」）、永不進日誌
  （沿用 SECURITY F-10 的遮罩原則）。
- 現有 `KeyManager`（4 把擁有者金鑰）改為**只給擁有者的背景 pipeline / 競技場**用，
  不給使用者互動端點用。

### R7-5　輸入驗證與速率限制
- 用 Pydantic 對所有 POST body 做 schema 驗證（型別、長度上限、必填）。
- 對 AI 端點加**每使用者**速率限制（即使 BYO，也要防單一使用者迴圈打爆自己/伺服器）。
- 沿用 2MB payload 上限。對應 SECURITY F-6。

### R7-6　安全基線（折入 SECURITY.md P1–P2）
- 全程 HTTPS（由部署平台/反向代理終結 TLS）。
- 統一錯誤回應：對外制式訊息 + 關聯 ID，細節只進伺服器日誌（SECURITY F-7）。
- 所有 POST 有 CSRF 對策（SameSite cookie + 同源檢查）（SECURITY F-5/P2-2）。
- 前端 `innerHTML` XSS 逐一稽核（SECURITY F-8/P2-5）——多人環境 XSS 可竊 session，
  風險升級，此項從「建議」升為「必做」。

### R7-7　遷移：SQLite → Postgres（不可破壞現有資料）
- 寫一次性遷移腳本：把 `data/serenity.sqlite` 的全域表匯入 Postgres；`user_memories`
  匯入時全部歸給擁有者的 user_id（或標記 legacy）。
- **遷移在複本上先跑**，驗證行數與抽樣值一致後才對正式資料執行；保留 SQLite 原檔備份。
- 遵守專案鐵律：零捏造、遷移冪等、可回滾。

### R7-8　部署與維運
- 部署文件：環境變數清單（OAuth client id/secret、DB URL、加密主金鑰）、DB 備份策略、
  健康檢查端點。
- 背景 pipeline（J-1~J-11 排程）改為對 Postgres 執行；擁有者金鑰走環境變數。

---

## 4. 目標資料模型（新增/變更部分）

```
users(id PK, provider, provider_user_id, email, display_name, status, created_at)
  unique(provider, provider_user_id)
user_api_keys(user_id FK, provider='gemini', ciphertext, key_suffix, updated_at)
  primary key(user_id, provider)
user_memories(... 既有欄位 ..., user_id FK NOT NULL)   -- 新增 user_id
user_chat_log(id PK, user_id FK, ts, prompt, response, prompt_tokens, completion_tokens, model)
-- 全域表：結構不變，僅遷移到 Postgres
```

---

## 5. API 契約變更（前端據此改，後端據此建）

- 新增：`GET /auth/login/{provider}`、`GET /auth/callback/{provider}`、`POST /auth/logout`、
  `GET /api/me`（回登入態與核准狀態，不含金鑰值）。
- 新增：`PUT /api/me/apikey`（設定 BYO 金鑰，body 加密前只走 HTTPS）、
  `DELETE /api/me/apikey`、`GET /api/me/apikey`（只回 `{configured:true, suffix:"...ab12"}`）。
- 變更：`/api/chat`、`/api/translate`、`/api/scorecard/generate/*`、`/api/memory*` →
  全部要求登入，內部改用「當前使用者的金鑰 + user_id 過濾」。
- 移除：`/api/monitor`（及 `dashboard/monitor.html`）。
- 不變：全域唯讀端點的回傳 schema 維持，前端僅需補上登入態與 401 處理。

---

## 6. 安全紅線（驗收必測，違反即退回）

1. 未登入存取私有端點 → 401。
2. 登入使用者 A 用任何手段（改 body、改 query、猜 id）**讀不到/改不到** B 的記憶或聊天。
3. `user_api_keys` 在 DB 中是密文；任何 API 回應、任何日誌都不含金鑰明文。
4. 錯誤回應不洩漏堆疊/路徑/SQL 細節。
5. 全站 HTTPS；session cookie 為 HttpOnly+Secure+SameSite。
6. 遷移後全域資料行數與抽樣值與 SQLite 原檔一致。

---

## 7. 誠實條款：監督者做得到與做不到

**做得到（在此環境內）**：寫規格與綁定契約、派 subagent 分階段實作、逐階段用 fresh-context
agent 驗收、寫遷移腳本並在複本上驗證、程式碼層級的安全審查。

**做不到 / 需要你親自做**（安全規則禁止我代做，或環境外的動作）：
- 到 Google/GitHub 開發者後台**申請 OAuth client id/secret**（要登入你的帳號、同意條款）。
- **註冊雲端平台/資料庫、輸入付款方式、設定 DNS/網域**。
- 把 OAuth secret、DB 連線字串、加密主金鑰**填入正式環境變數**（機密輸入你自己來）。
- 決定邀請名單、隱私政策文字。

我會在需要這些時，明確給你「一步步該點哪裡」的指引，由你操作。

---

## 8. 分階段實作計畫（每階段可獨立驗收）

> 每階段：監督者寫該階段驗收測試 → 派 Sonnet subagent（worktree 隔離）實作 →
> fresh-context agent 驗收 → 監督者逐行審 diff → 本地 merge。全程不碰正式資料，
> 用複本或測試 DB。

- **P7-0　技術決策確認 + 骨架**：確認 FastAPI+Postgres+Authlib；建最小可跑骨架
  （一個公共唯讀端點 + 健康檢查），本機 Postgres（Docker）跑起來。**先不接 OAuth**。
- **P7-1　OAuth 登入 + users + 邀請制**：Google/GitHub 登入、session、`/api/me`、
  邀請/核准。驗收：能登入、未核准看不到私有功能。
- **P7-2　資料遷移 SQLite→Postgres**：全域表遷移腳本 + 驗證；`user_memories` 加 user_id。
  驗收：行數/抽樣一致、可回滾。
- **P7-3　BYO 金鑰 + 私有端點隔離**：金鑰加密儲存、`call_gemini` 改用使用者金鑰、
  聊天/記憶綁 user_id。驗收：第 6 節紅線 1–3 全過。
- **P7-4　安全基線**：統一錯誤、CSRF、速率限制、XSS 稽核、移除 monitor。驗收：紅線 4–5。
- **P7-5　部署**：擁有者操作雲端/DNS/secrets（監督者給指引）、pipeline 指向 Postgres、
  上線煙測。驗收：HTTPS 可達、健康檢查綠、一個真實使用者走完登入→設金鑰→聊天。

**里程碑建議**：先做到 P7-3 在**本機**完整跑通（多帳號隔離驗證過）再談 P7-5 對外部署——
不要在隔離都還沒驗證前就開門。
