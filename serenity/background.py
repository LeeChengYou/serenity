"""
serenity/background.py
run_background_ingest — in-process 版

改用 importlib 動態載入 scripts/ingest.py，直接呼叫 fetch_prices() 函式；
不透過外部 process 啟動（打包後外部啟動會無限自我啟動）。
"""
import importlib.util
import time

from .config import ROOT
from .services.signal import snapshot_signals
from .services.chat import decay_memories


def _load_ingest():
    """動態載入 ROOT/scripts/ingest.py，回傳模組物件（仿 serenity/quant.py 模式）。"""
    spec = importlib.util.spec_from_file_location("ingest", ROOT / "scripts" / "ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_background_ingest():
    """每 12 小時 in-process 呼叫 ingest.fetch_prices()；snapshot/decay 呼叫不變。"""
    # Wait 10 seconds after server starts
    time.sleep(10)
    while True:
        try:
            print("[Scheduler] Starting automatic incremental price fetch (in-process)...")
            ingest = _load_ingest()
            ingest.fetch_prices()
            print("[Scheduler] Automatic incremental price update completed successfully.")

            # R-5: snapshot today's signals for hit-rate tracking
            try:
                snapshot_signals()
            except Exception as snap_exc:
                print(f"[Scheduler] snapshot_signals warning: {snap_exc}")

            # Apply memory decay daily
            decay_memories()
        except Exception as e:
            print(f"[Scheduler] Background ingest warning: {e}")

        # Run every 12 hours
        time.sleep(12 * 3600)
