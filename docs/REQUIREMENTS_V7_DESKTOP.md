# V7 規格：server.py 模組化 + 可散佈桌面 App（設定視窗版）

> 目標：讓非開發者拿到安裝檔就能用——自己在設定視窗貼上 Gemini API key，
> 不需要 .env、不需要 Python 環境。同時把 3,129 行的 server.py 拆成可維護的套件。
> 本規格分三階段，每階段獨立派工、獨立驗收、獨立 merge。

## 0. 架構參照（商業化專案的慣例，本規格仿造對象）

| 慣例 | 參照專案 | 本專案對應 |
|------|----------|-----------|
| Python 套件化 + 分層（api / services / db / config 分離） | FastAPI 官方 bigger-applications 佈局、Jupyter Server、Datasette | `serenity/` 套件，見 §1 |
| 使用者設定存 user 目錄，不放程式目錄 | Streamlit（`~/.streamlit/config.toml`）、Jupyter（`~/.jupyter/`） | `%LOCALAPPDATA%\Serenity\config.json`，見 §2 |
| 首次啟動引導使用者填 API key，UI 內設定視窗 | Open WebUI、LM Studio、Cherry Studio | 儀表板齒輪⚙設定 modal，見 §2 |
| 本地 server + 原生視窗殼 + 單檔打包 | pywebview 官方範例、Tauri sidecar 模式 | pywebview + PyInstaller onedir，見 §3 |
| 金鑰遮罩顯示、永不回傳明文 | 所有商業後台（如 OpenAI dashboard 的 `sk-...abcd`） | 見 §2 安全規則 |

刻意**不做**的事（控制風險）：不換 web 框架（維持 stdlib `http.server`，
框架替換屬未來議題，本次拆模組時把路由集中在一個檔案，未來要換 FastAPI 只動那一檔）；
不做手機原生 app；不把 X 抓取（Playwright/x_curl）帶進散佈版（標為開發者功能，App 缺推文資料時優雅降級）。

---

## 階段一：server.py 模組化（行為零改變的搬家）

### 1.1 目標佈局

```
serenity/                      # 新 Python 套件（repo 根目錄下）
  __init__.py                  # __version__ = "7.0.0"
  config.py                    # ROOT、DB_PATH、STATIC_DIR、.env 載入（原 server.py 70-89 行搬入）
  db.py                        # db()、_init_schema、_schema_initialized、_table_exists、one()
  keypool.py                   # KeyManager 類 + _key_manager 單例
  gemini.py                    # call_gemini()
  quant.py                     # indicators/signals/score_serenity_stock 動態載入
                               #（_compute_indicators、_compute_ema、_evaluate_signal、_quant_score）
  services/
    __init__.py
    market.py                  # summary、symbol_payload、news_payload、fundamentals_payload、
                               # estimates_payload、changes_payload
    signal.py                  # signal_payload、snapshot_signals
    regime.py                  # _last_non_none、_benchmark_block、regime_payload
    hitrate.py                 # _hitrate_lock、_compute_live_hitrate、_compute_reconstructed_hitrate、hitrate_payload
    experts.py                 # _expert_views_row_to_item、expert_views_payload、expert_views_all_payload
    dossier.py                 # _RELIABILITY_NOTE、dossier_payload
    scorecard.py               # 從 route_post_api 651-844 行抽出的 scorecard 生成邏輯（唯一允許的「抽函式」）
    chat.py                    # handle_chat_api、log_chat_transaction、consolidate_memory_in_background、
                               # extract_memory_task、decay_memories
    translate.py               # handle_translate_api
    arena_views.py             # arena_leaderboard/nav/trades/reflections_payload
  api/
    __init__.py
    handler.py                 # Handler 類、send_json、route_api、route_post_api（只做分發，邏輯全在 services）
  background.py                # run_background_ingest
  app.py                       # main()：argparse（--host/--port/--snapshot-once 不變）、組裝啟動
scripts/server.py              # 變成薄 shim（見 1.2），保留檔案不刪
```

### 1.2 向後相容（鐵則，違反=退回）

`scripts/server.py` shim 必須：
1. 把 repo root 加進 `sys.path` 後 re-export 下列名稱（`import server` 的舊呼叫者不能壞）：
   `call_gemini`、`_key_manager`、`db`、`signal_payload`、`snapshot_signals`、
   `arena_leaderboard_payload`、`arena_nav_payload`、`arena_trades_payload`、
   `arena_reflections_payload`、`DB_PATH`、`ROOT`。
   （已知呼叫者：`agent_arena.py:661` 的 `_srv._key_manager`/`_srv.call_gemini`；
   `scratch/test_arena_final.py:334`；`scratch/test_api_call.py:11`）
2. `if __name__ == "__main__"` 時呼叫 `serenity.app.main()`——
   `python scripts\server.py --port 8787` 與 `--snapshot-once` 的行為與輸出完全不變
   （schtasks J-2、daily_check.py、catchup.py 依賴這個 CLI）。
3. 單例唯一性：`_key_manager` 與 `_schema_initialized` 全 repo 只能有一份實例
   （shim re-export 同一物件，不得重新實例化）。

### 1.3 搬家規則

- **只搬不改**：函式內容逐字搬移，只允許改 import 與模組前綴；唯一例外是
  scorecard 生成邏輯從 route_post_api 抽成 `services/scorecard.py` 的具名函式。
- 循環依賴的解法方向：dossier → signal/regime/estimates 是單向的，照依賴序排模組即可；
  api/handler.py import 全部 services，services 之間只允許 dossier 依賴其他 service。
- 不新增任何第三方依賴；不動 dashboard/、ingest.py、agent_arena.py、daily_check.py。

### 1.4 階段一驗收（全過才算完成）

- [ ] `PYTHONIOENCODING=utf-8 python scratch/test_server_refactor.py` 全過
      （對照 `scratch/baseline_api.json`：23 個 GET 端點的 status code 與 JSON 頂層 key 集合一致）
- [ ] `PYTHONIOENCODING=utf-8 python scratch/test_arena_final.py` → 0 failed（測試檔不得修改）
- [ ] `PYTHONIOENCODING=utf-8 python scratch/test_daily_check.py` → 0 failed
- [ ] `python scripts/server.py --snapshot-once` 正常執行且輸出格式不變
- [ ] `python -m py_compile` 全部新模組 + shim
- [ ] `scripts/server.py` 縮到 ≤ 60 行

---

## 階段二：設定系統 + 設定視窗（散佈版的靈魂）

### 2.1 設定解析順序（config.py 擴充）

每個設定值依序取第一個存在者：
1. CLI 參數（如 `--db`、`--port`）
2. 環境變數（含 .env——開發者模式照舊可用）
3. `SERENITY_HOME/config.json`（使用者透過設定視窗寫入）
4. 內建預設值

`SERENITY_HOME`：環境變數 `SERENITY_HOME` 優先；否則 Windows 用
`%LOCALAPPDATA%\Serenity`、其他平台 `~/.serenity`。**開發模式例外**：
從 repo 原始碼執行且 `ROOT/data/serenity.sqlite` 存在時，DB 與 log 沿用 repo 路徑
（現有排程、測試全部不變）。

`config.json` schema（缺欄=用預設）：
```json
{
  "gemini_api_key": "", "gemini_api_key_2": "", "gemini_api_key_3": "", "gemini_api_key_4": "",
  "gemini_model": "gemini-2.5-flash",
  "gemini_translate_model": "gemini-2.5-flash-lite",
  "gemini_memory_model": "gemini-2.0-flash-lite"
}
```

### 2.2 設定 API

- `GET /api/settings` → `{"has_key": bool, "keys": [{"slot": 1, "masked": "AIza…x7Qk", "set": true}, ...], "models": {...}, "config_path": "..."}`
  —— key 一律遮罩（前 4 + 後 4 字元），**任何回應、log、錯誤訊息都不得出現完整 key**。
- `POST /api/settings` body `{"gemini_api_key": "...", "gemini_model": "..."}`（部分更新；空字串=清除該 slot）
  → 寫入 `config.json` → `KeyManager.reload()`（KeyManager 新增 reload 方法，執行緒安全，沿用既有 RLock）→ 回傳與 GET 相同的遮罩狀態。
- `POST /api/settings/test` body `{"key": "..."}` → 用該 key 打
  `GET https://generativelanguage.googleapis.com/v1beta/models?key=...`（不耗生成額度）
  → 回 `{"ok": true}` 或 `{"ok": false, "error": "HTTP 400/403 說明"}`。key 不落地、不落 log。

### 2.3 設定視窗（dashboard/）

- 頂欄加齒輪 ⚙ 按鈕 → 設定 modal（zh-TW）：4 個 key 輸入框（`type=password`，
  已設定的顯示遮罩 placeholder）、模型三個下拉、「測試連線」、「儲存」。
- 首次啟動偵測：`/api/settings` 回 `has_key=false` 時自動彈出 modal，
  附一段引導文字（去哪申請 Google AI Studio 免費 key、貼上即可）。
- 未設 key 時，聊天/翻譯/記分卡生成等 AI 功能顯示「請先在設定填入 API key」提示，
  不噴 500；行情/訊號/競技場檢視等非 AI 功能照常可用。

### 2.4 階段二驗收

- [ ] 新驗收測試 `scratch/test_settings.py` 全過：
      以 `SERENITY_HOME=暫存目錄`、清空 GEMINI 環境變數啟動 server →
      GET has_key=false；POST 假 key `AIzaFAKE_TEST_KEY_1234` → config.json 寫入、
      GET 回遮罩 `AIza…1234`、`/api/keypool` 看得到 1 把 key；
      全部 GET 端點回應文字中不得出現完整假 key；重複 POST 冪等。零真實 Gemini 呼叫。
- [ ] test_arena_final / test_daily_check / test_server_refactor 仍全過（.env 開發模式不受影響）
- [ ] 重啟 server + Ctrl-F5 人工驗證 modal（回報截圖或 DOM 檢查輸出）

---

## 階段三：桌面殼 + 打包

### 3.1 元件

- `serenity/desktop.py`：挑空閒 port → 背景執行緒啟動 server → pywebview 原生視窗
  指向 `http://127.0.0.1:{port}`，關窗即退出。單例（重複開啟聚焦既有視窗，用本機 port 檔實現）。
- **背景管線改為 in-process**：`background.py` 不得再用 `subprocess([sys.executable, "ingest.py"…])`
  （打包後 `sys.executable` 是 exe 自己，會無限自我啟動）——改成 `import ingest` 後直接呼叫
  `fetch_prices()` 等函式；開發模式與打包模式共用同一條路徑。
- 首次啟動（DB 全空）時 UI 顯示 onboarding：填 key → 按「抓取初始資料」→
  `POST /api/admin/bootstrap` 觸發 in-process 抓 prices/benchmarks/news（進度以 job_runs 表回報）。
- `packaging/desktop.spec`（PyInstaller onedir）+ `scripts/build_desktop.ps1`：
  bundle `dashboard/`、`scripts/indicators.py`、`scripts/signals.py`、scorer skill；
  **絕不 bundle**：`.env`、`x_curl/`、`data/`、`docs/`。新依賴：`pywebview`、`pyinstaller`（僅 build 機）。

### 3.2 階段三驗收

- [ ] 開發模式 `python -m serenity.desktop` 開出視窗、儀表板可操作（人工冒煙）
- [ ] `scripts/build_desktop.ps1` 產出 `dist/Serenity/Serenity.exe`；
      在乾淨的 `SERENITY_HOME`（暫存目錄）下啟動 exe：出現 onboarding、
      填假 key 可儲存（config.json 出現在暫存目錄）、無 .env/x_curl 打包進 dist（用檔案清單證明）
- [ ] 既有全部測試仍過

---

## 通用鐵律（每階段派工 prompt 都要帶）

零捏造數據；零 look-ahead；機密不落 git 不落 log（key 遮罩規則見 §2.2）；
測試用 StubBackend/假 key，不打真 Gemini；DB 是 data/serenity.sqlite；
PYTHONIOENCODING=utf-8；worktree 沒有 data/，要先從主 repo 拷貝 DB。
