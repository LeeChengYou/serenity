# Graph Report - .  (2026-07-14)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 648 nodes · 1295 edges · 70 communities (51 shown, 19 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 25 edges (avg confidence: 0.72)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ae88c127`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- app.js
- deep_dive.py
- handle_chat_api
- test_tw_phase1.py
- test_tw_phase2.py
- get_setting
- Handler
- KeyManager
- handler.py
- market.py
- test_indicators.py
- health.py
- .route_post_api
- test_news_page.py
- bootstrap.py
- config.py
- signal.py
- app.py
- test_signal_rsi_field.py
- manifest.json
- hitrate.py
- regime_payload
- 模型調度守則 (DISPATCH)
- test_pwa_auth.py
- watchlist.py
- _HTTPResponse
- dossier_payload
- test_arena_final.py
- test_desktop.py
- test_health.py
- test_product_p0.py
- test_server_refactor.py
- test_settings.py
- Serenity Signal — 股票分析推薦平台 完整規格書
- credibility_analysis.py
- REQUIREMENTS V7 — 多人上線架構規格
- test_context_trim.py
- test_incremental.py
- test_memory.py
- parse_curl_test
- test_semantic_search.py
- sw.js
- batch_scorecards.sh
- batch_scorecards_retry.sh
- Current Stack
- Project Overview
- Serenity.skill
- Environment Traps
- Iron Rules
- Dashboard Index
- QR Code for Subscription
- AI Market Analysis Requirements
- Serenity Signal — 多窗口樣本外驗證報告
- Serenity Stock Scorer Specification

## God Nodes (most connected - your core abstractions)
1. `$()` - 60 edges
2. `escapeHtml()` - 32 edges
3. `json()` - 32 edges
4. `get_setting()` - 25 edges
5. `Handler` - 20 edges
6. `main()` - 19 edges
7. `deep_dive_payload()` - 18 edges
8. `db()` - 17 edges
9. `init()` - 16 edges
10. `selectSymbol()` - 16 edges

## Surprising Connections (you probably didn't know these)
- `_news_route()` --indirect_call--> `Handler`  [INFERRED]
  scratch/test_news_page.py → serenity/api/handler.py
- `_make_local_llm_server()` --indirect_call--> `Handler`  [INFERRED]
  scratch/test_tw_phase2.py → serenity/api/handler.py
- `test_expert_views_regression()` --calls--> `expert_views_all_payload()`  [EXTRACTED]
  scratch/test_news_page.py → serenity/services/experts.py
- `_make_fixture_server()` --indirect_call--> `Handler`  [INFERRED]
  scratch/test_tw_directory.py → serenity/api/handler.py
- `test_tw_search_api()` --indirect_call--> `Handler`  [INFERRED]
  scratch/test_tw_directory.py → serenity/api/handler.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Data Ingestion & Processing Flow** — scripts_ingest, data_serenity_sqlite, scripts_daily_check, docs_roadmap [EXTRACTED 0.95]
- **AI Agent Arena Ecosystem** — scripts_agent_arena, docs_requirements_v6, docs_requirements_fund_pool, data_serenity_sqlite [EXTRACTED 0.90]
- **Serenity Research Methodology** — agents_skills_serenity_skill_skill, agents_skills_serenity_stock_scorer_skill, agents_skills_serenity_stock_scorer_scripts_score_serenity_stock [EXTRACTED 0.85]
- **Agent Governance Framework** — docs_agents_dispatch, docs_agents_judgment, docs_agents_maintenance, docs_agents_prompts, docs_agents_lessons, docs_agents_harness_diagnosis [EXTRACTED 1.00]
- **Serenity V7 Multi-tenant & Desktop Architecture** — docs_requirements_v7, docs_requirements_v7_desktop, docs_security [EXTRACTED 1.00]

## Communities (70 total, 19 thin omitted)

### Community 0 - "app.js"
Cohesion: 0.05
Nodes (104): $(), _activeTab(), appendChatMessage(), applyChangeBadgesToSymbols(), applyTimeRange(), ARENA_COLORS, arenaColorFor(), _arenaHidden (+96 more)

### Community 1 - "deep_dive.py"
Cohesion: 0.07
Nodes (49): approx(), check(), finish(), main(), _make_llm_handler(), Wilder RSI-14（對應 agent_arena._calc_rsi14）。, EMA（對應 agent_arena._calc_ema）。, Wilder ATR-14（獨立實作，對應規格）。 (+41 more)

### Community 2 - "handle_chat_api"
Cohesion: 0.08
Nodes (30): Serenity Stock Scorer, Serenity SQLite Database, Fund Pool Requirements, AI Agent Arena Requirements, check(), finish(), main(), check() (+22 more)

### Community 3 - "test_tw_phase1.py"
Cohesion: 0.10
Nodes (29): approx(), check(), finish(), main(), _mark_all_unrun(), import 失敗時，把尚未執行的測項全部標記為 FAIL（輸出完整測項清單）, check(), _fake_yahoo_chart() (+21 more)

### Community 4 - "test_tw_phase2.py"
Cohesion: 0.16
Nodes (24): HTTPServer, check(), finish(), main(), _make_handler(), _openai_response(), _start_server(), check() (+16 more)

### Community 5 - "get_setting"
Cohesion: 0.16
Nodes (19): get_setting(), 解析順序：環境變數 > config.json > 預設值。     name 是 _VALID_KEYS 中的設定名稱。, db(), _init_schema(), serenity/db.py db(), _init_schema, _schema_initialized, _table_exists, one() （原, call_gemini(), serenity/gemini.py call_gemini()（原 server.py 259-303 行）, Unified Gemini API call with KeyManager 429 failover routing. (+11 more)

### Community 6 - "Handler"
Cohesion: 0.18
Nodes (18): check(), _make_fixture_server(), _make_temp_db(), Connection, 啟動本地假 HTTP server。     - GET /twse → twse_items（JSON）；狀態碼 twse_status     - GET, test_c3_etf_directory(), test_c3_etf_failure(), test_c3_migrate_kind_column() (+10 more)

### Community 7 - "KeyManager"
Cohesion: 0.11
Nodes (11): KeyManager, Preferred label order for a given task class.          agent_arena: round-robin, Thread-safe Gemini API key pool.      Task affinity:       interactive → KEY_1, Return the best available (non-cooling, non-excluded) key entry., Shared logic for 429/503: record error and set cooling., Record a 429 and update cooling state for the given entry., Record a 503 and update cooling state (same policy as 429)., Unix timestamp of next midnight in US/Pacific time. (+3 more)

### Community 8 - "handler.py"
Cohesion: 0.14
Nodes (19): _ensure_dd_migrate(), serenity/api/handler.py Handler 類、send_json、route_api、route_post_api （原 server.p, arena_leaderboard_payload(), arena_nav_payload(), arena_reflections_payload(), arena_trades_payload(), serenity/services/arena_views.py arena_leaderboard_payload, arena_nav_payload, a, GET /api/arena/leaderboard?month=YYYY-MM     Live leaderboard computed directly (+11 more)

### Community 9 - "market.py"
Cohesion: 0.14
Nodes (18): one(), Return True if the named table exists in the database., _table_exists(), expert_views_all_payload(), expert_views_payload(), _expert_views_row_to_item(), serenity/services/experts.py _expert_views_row_to_item, expert_views_payload, ex, GET /api/expert-views/<SYM>     Returns up to 10 items mentioning this symbol, n (+10 more)

### Community 10 - "test_indicators.py"
Cohesion: 0.20
Nodes (15): assert_close(), assert_true(), BB(3, 2σ) on [10, 20, 30].      SMA = 20, variance = ((10-20)^2+(20-20)^2+(30-20, ATR(2) on 3 bars.      bar0: H=12, L=10, C=11   — first bar, TR = H-L = 2     ba, volume_ratio with avg_period=3.      bars (reversed): [10, 5, 5, 20, ...]     la, EMA(3) on [1,2,3,4,5] with k=0.5.      seed (SMA of first 3) = (1+2+3)/3 = 2.0, RSI(3) on a 5-bar series.      Changes:  [+1, +1, +1, -1]     After 3 changes (p, real_data_sanity() (+7 more)

### Community 11 - "health.py"
Cohesion: 0.16
Nodes (16): _ensure_job_runs_table(), _load_daily_check(), _load_ingest(), Connection, 確保 job_runs 表存在（欄位與 daily_check.py 一致）。, 將結果記錄到 job_runs 表（in-process 呼叫；cmd 標記為 in-process）。, 執行單一安全域的補抓動作，更新步驟狀態並寫 job_runs。, 背景執行緒：依序補抓各域，完成後解除 running 標記。 (+8 more)

### Community 12 - ".route_post_api"
Cohesion: 0.18
Nodes (14): _load_ingest(), plan_auto_refresh(), serenity/background.py run_background_ingest — in-process 版  改用 importlib 動態載入 s, 動態載入 ROOT/scripts/ingest.py，回傳模組物件（仿 serenity/quant.py 模式）。, 唯讀計算目前過期∩SAFE 域，回傳應自動補抓的域清單（依 SAFE_DOMAINS 順序）。     供背景排程與測試使用；不執行任何寫入。, 每 60 分鐘一輪自動補抓：     - plan_auto_refresh() 非空 → 呼叫 run_refresh(清單, "auto")     - 每, run_background_ingest(), decay_memories() (+6 more)

### Community 13 - "test_news_page.py"
Cohesion: 0.30
Nodes (13): check(), _insert_news(), _make_temp_db(), _news_route(), Connection, Fresh empty tempfile DB with minimal schema for news tests.     We intentionally, Call handler.route_api with a fresh connection (route_api closes it)., Bulk-insert news rows. Each dict: title, source, url, published_at, scope, symbo (+5 more)

### Community 14 - "bootstrap.py"
Cohesion: 0.21
Nodes (12): get_status(), handle_post_bootstrap(), _load_ingest(), serenity/services/bootstrap.py Bootstrap API — 首次啟動時抓取初始資料（prices / benchmarks /, 回傳目前 bootstrap 狀態（thread-safe 快照）。, POST /api/admin/bootstrap 主邏輯。     - dry_run=true  → 回傳計畫步驟，不執行     - otherwise, 動態載入 ROOT/scripts/ingest.py（仿 quant.py 模式）。, 將結果記錄到 job_runs 表（mode="bootstrap"）。 (+4 more)

### Community 15 - "config.py"
Cohesion: 0.14
Nodes (15): _get_serenity_home(), load_config(), Path, serenity/config.py ROOT, DB_PATH, STATIC_DIR, .env 載入（原 server.py 70-89 行） + 階段二, 讀取 SERENITY_HOME/config.json，缺欄填預設值。回傳完整 dict。, 部分更新 config.json（只覆蓋傳入欄位）。空字串 = 清除該欄。, 解析 SERENITY_HOME 路徑（env 優先；否則平台預設）。, save_config() (+7 more)

### Community 17 - "app.py"
Cohesion: 0.23
Nodes (9): packaging/desktop_entry.py PyInstaller onedir 入口點。, main(), serenity/app.py main()：argparse（--host/--port/--snapshot-once 不變）、組裝啟動 （原 server, main(), _pick_free_port(), serenity/desktop.py 桌面殼：背景執行緒啟動 ThreadingHTTPServer，pywebview 原生視窗指向 localhost。, 在背景執行緒啟動 ThreadingHTTPServer，回傳 server 物件。, _start_server() (+1 more)

### Community 18 - "test_signal_rsi_field.py"
Cohesion: 0.38
Nodes (9): _make_bars(), ok(), Simulate the snapshot_signals() RSI extraction logic from server.py.     Primary, Generate n synthetic OHLCV bars (oldest-first) with a small upward drift.      P, Parse RSI value from the condition text (old fragile path).     Returns None if, _rsi_from_conditions(), test_rsi_field_none_when_insufficient(), test_rsi_field_present_when_data_sufficient() (+1 more)

### Community 19 - "manifest.json"
Cohesion: 0.22
Nodes (8): background_color, description, display, icons, name, short_name, start_url, theme_color

### Community 20 - "hitrate.py"
Cohesion: 0.28
Nodes (8): _compute_live_hitrate(), _compute_reconstructed_hitrate(), hitrate_payload(), Path, serenity/services/hitrate.py _hitrate_lock, _compute_live_hitrate, _compute_reco, Reconstruct hit rates using the multiwindow point-in-time machinery.      Reuses, Compute hit rates from live signal_history rows (accumulated since 2026-07-04)., GET /api/hitrate      Returns hit-rate analysis from two honestly labeled source

### Community 21 - "regime_payload"
Cohesion: 0.36
Nodes (7): _benchmark_block(), _last_non_none(), serenity/services/regime.py _last_non_none, _benchmark_block, regime_payload （原, Return the last non-None value in a series., EMA200 stance for one benchmark ETF: {"close","ema200","above"} or None., GET /api/regime  (contract: REQUIREMENTS_V3.md R3-2)      Regime rule (spec):, regime_payload()

### Community 22 - "模型調度守則 (DISPATCH)"
Cohesion: 0.29
Nodes (7): 模型調度守則 (DISPATCH), Harness 診斷書, 判斷力手冊 (JUDGMENT), 教訓日誌 (LESSONS), 給未來 session 的信, 制度檔維護協議 (MAINTENANCE), 派工 Prompt 模板 (PROMPTS)

### Community 23 - "test_pwa_auth.py"
Cohesion: 0.52
Nodes (6): http_get(), http_post(), lan_ip(), main(), record(), wait_port()

### Community 24 - "watchlist.py"
Cohesion: 0.38
Nodes (6): handle_get_watchlist(), handle_post_watchlist(), serenity/services/watchlist.py watchlist CRUD API handlers GET /api/watchlist  →, Build the watchlist payload with mention counts., POST /api/watchlist     Returns (response_dict, status_code).     Raises ValueEr, _watchlist_payload()

### Community 25 - "_HTTPResponse"
Cohesion: 0.33
Nodes (5): _BadRequest, _HTTPResponse, Exception, 路由回傳 400 Bad Request 時拋出。, 路由需要自訂 HTTP status code 時拋出（payload, status）。

### Community 26 - "dossier_payload"
Cohesion: 0.33
Nodes (6): dossier_payload(), R-3: Build (or return cached) the /api/dossier/<SYM> response.      Assembles al, estimates_payload(), GET /api/estimates/<SYM>      Returns analyst estimates from the analyst_estimat, Build the /api/signal/<SYM> response (SPEC F-06 / F-07).      Pulls real OHLCV b, signal_payload()

### Community 27 - "test_arena_final.py"
Cohesion: 0.70
Nodes (4): approx(), check(), finish(), main()

### Community 28 - "test_desktop.py"
Cohesion: 0.70
Nodes (4): http(), main(), record(), wait_port()

### Community 29 - "test_health.py"
Cohesion: 0.70
Nodes (4): http(), main(), record(), wait_port()

### Community 30 - "test_product_p0.py"
Cohesion: 0.70
Nodes (4): http(), main(), record(), wait_port()

### Community 31 - "test_server_refactor.py"
Cohesion: 0.70
Nodes (4): fetch(), main(), shape_of(), wait_port()

### Community 32 - "test_settings.py"
Cohesion: 0.70
Nodes (4): http(), main(), record(), wait_port()

### Community 33 - "Serenity Signal — 股票分析推薦平台 完整規格書"
Cohesion: 0.50
Nodes (4): Serenity Signal Ledger Dashboard UI, V7 規格：server.py 模組化 + 可散佈桌面 App, Serenity Signal — 股票分析推薦平台 完整規格書, Serenity.skill Specification

### Community 34 - "credibility_analysis.py"
Cohesion: 0.83
Nodes (3): fwd_return(), load(), main()

### Community 36 - "REQUIREMENTS V7 — 多人上線架構規格"
Cohesion: 0.67
Nodes (3): REQUIREMENTS V7 — 多人上線架構規格, Project Roadmap, Serenity Signal — 資訊安全評估與改善計畫

## Knowledge Gaps
- **43 isolated node(s):** `state`, `_xlate`, `LW_THEME`, `_changesData`, `_settingsOriginal` (+38 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **19 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Handler` connect `Handler` to `test_tw_phase2.py`, `handler.py`, `.route_post_api`, `test_news_page.py`, `app.py`?**
  _High betweenness centrality (0.055) - this node is a cross-community bridge._
- **Why does `deep_dive_payload()` connect `deep_dive.py` to `handler.py`, `test_tw_phase2.py`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Why does `market_board_payload()` connect `test_tw_phase1.py` to `handler.py`, `handle_chat_api`, `get_setting`, `Handler`?**
  _High betweenness centrality (0.039) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `Handler` (e.g. with `_news_route()` and `_make_fixture_server()`) actually correct?**
  _`Handler` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `state`, `_xlate`, `LW_THEME` to the rest of the system?**
  _43 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `app.js` be split into smaller, more focused modules?**
  _Cohesion score 0.054840416152354084 - nodes in this community are weakly interconnected._
- **Should `deep_dive.py` be split into smaller, more focused modules?**
  _Cohesion score 0.07450980392156863 - nodes in this community are weakly interconnected._