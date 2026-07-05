# Serenity Signal V5 — Playwright 爬蟲基礎設施需求規格

> 版本：v1.0 | 建立：2026-07-05 | 整理者：Fable（監督者）
> 目標：(1) 解決 A-3 X cookie 手動更新問題；(2) 建立可延伸的
> 爬蟲框架，未來納入**有公信力**的股票經理人觀點來源。

---

## R5-1 Playwright 持久化瀏覽器設定檔（X session 自動維護）

### 架構

- 專用瀏覽器 profile：`data/browser_profile/`（**必須加入 .gitignore**，
  內含 session cookie，絕不可提交）
- `python scripts/crawler.py login`：開**有頭**瀏覽器至 x.com，
  使用者手動登入一次後關閉；session 存入 profile
- `python scripts/crawler.py refresh-cookies`：**無頭**模式帶 profile
  訪問 x.com，確認登入態，將最新 cookies（auth_token、ct0 等）
  改寫進 `x_curl/` 既有檔案的 Cookie header 與 x-csrf-token header
  → 既有 `ingest.py fetch-x` 管線完全不用改
- 未登入/登入失效時：明確 zh-TW 提示「請執行 login 重新登入」，
  exit code 非 0，不得靜默失敗

### 排程整合

- J-4 從「每週手動」升級為：`crawler.py refresh-cookies && ingest.py fetch-x`
  每週一次（schtasks 註冊行寫入 ROADMAP）
- 禮貌原則：頻率維持每週、僅抓公開頁面、請求間隔 ≥2 秒

### 安全鐵律

1. 絕不經手使用者密碼——登入由使用者在有頭瀏覽器親自完成
2. cookie/token 值不進 log、不進 git（profile 目錄 + x_curl/ 均 gitignore；
   x_curl 現況確認：若未 ignore 則本輪補上）
3. 帳號風險已告知使用者（ToS），節流降險

## R5-2 可延伸來源框架（未來：有公信力的經理人觀點）

### 設計

`scripts/crawler.py` 內建 source registry：

```python
class Source:            # 基底
    name: str
    credibility: str     # "official" | "aggregator" | "individual"
    requires_login: bool
    def fetch(self, ctx) -> list[dict]: ...   # 回標準化 items
```

標準化 item：`{source, author, title, text, url, published_at, symbols, fetched_at}`
落地新表 `expert_views(id, source, author, title, text, url unique,
published_at, symbols, credibility, fetched_at)`。

### 首發來源（本輪實作一個，證明框架可延伸）

**SEC EDGAR 13F**（credibility="official"，免登入、官方 API、最高公信力）：
- 抓取知名基金經理人季度持倉（先收錄 3-5 位：如 Berkshire、
  Scion、Appaloosa 等，config 可增減 CIK）
- EDGAR full-text/submissions API，遵守 SEC 要求的 User-Agent 聲明
- 產出：每季持倉變化（新建倉/加倉/清倉）→ expert_views 表
- **公信力說明**：13F 是法定申報文件，但有 45 天延遲——
  UI 呈現時必須標示申報期與延遲

### 未來擴充（本輪不做，框架預留）

- Dataroma 超級投資人持倉聚合（aggregator）
- 基金致股東信（official）
- 其他 X 財經帳號（individual，多帳號三角驗證 = roadmap C-6）

## R5-3 前端（輕量）

- 側欄或 dossier 內新增「專家觀點」小卡：該股票若出現在
  expert_views（如某 13F 新建倉），列出來源+動作+申報期，
  標示 credibility 徽章（官方/聚合/個人）與延遲警語
- 無資料時不顯示該卡（不佔版面）

## 驗收原則

1. 密碼零經手、cookie 零入庫零入 git
2. 未登入時全管線優雅降級 + 明確指引
3. 13F 資料零捏造：全部欄位可追溯 EDGAR 原始文件 URL
4. 延遲/公信力誠實標示
