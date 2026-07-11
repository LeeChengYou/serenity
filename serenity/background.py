"""
serenity/background.py
run_background_ingest — in-process 版

改用 importlib 動態載入 scripts/ingest.py，直接呼叫 fetch_prices() 函式；
不透過外部 process 啟動（打包後外部啟動會無限自我啟動）。

V7 更新：
- 新增 plan_auto_refresh()：唯讀計算目前過期∩SAFE 域（供背景排程與測試共用）。
- run_background_ingest()：改為每 60 分鐘一輪；plan_auto_refresh() 非空才補抓；
  原本每 12 小時無條件 fetch_prices 廢除（過期才抓，prices 門檻是交易日，效果等價）。
  每天第一輪額外跑 decay_memories()。
"""
import importlib.util
import time
from datetime import datetime, timezone

from .config import ROOT
from .services.chat import decay_memories


def _load_ingest():
    """動態載入 ROOT/scripts/ingest.py，回傳模組物件（仿 serenity/quant.py 模式）。"""
    spec = importlib.util.spec_from_file_location("ingest", ROOT / "scripts" / "ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plan_auto_refresh() -> list:
    """
    唯讀計算目前過期∩SAFE 域，回傳應自動補抓的域清單（依 SAFE_DOMAINS 順序）。
    供背景排程與測試使用；不執行任何寫入。
    """
    from .services.health import get_stale_domains
    safe_stale, _ = get_stale_domains()
    return safe_stale


def run_background_ingest():
    """
    每 60 分鐘一輪自動補抓：
    - plan_auto_refresh() 非空 → 呼叫 run_refresh(清單, "auto")
    - 每天第一輪額外跑 decay_memories()
    函式名與簽名不變（app.py/desktop.py 呼叫處不用動）。
    """
    # 啟動後延遲 10 秒
    time.sleep(10)

    last_decay_date = None

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today_date = now_utc.date()

            # 每天第一輪跑 decay_memories
            if last_decay_date != today_date:
                try:
                    decay_memories()
                    last_decay_date = today_date
                except Exception as decay_exc:
                    print(f"[Scheduler] decay_memories warning: {decay_exc}")

            # 計算需補抓的域
            domains = plan_auto_refresh()
            if domains:
                print(f"[Scheduler] Auto-refresh triggered for domains: {domains}")
                from .services.health import run_refresh, is_running
                if not is_running():
                    run_refresh(domains, "auto")
                else:
                    print("[Scheduler] Refresh already running, skipping this cycle.")
            else:
                print("[Scheduler] All domains fresh, nothing to auto-refresh.")

        except Exception as e:
            print(f"[Scheduler] Background auto-refresh warning: {e}")

        # 每 60 分鐘一輪
        time.sleep(60 * 60)
