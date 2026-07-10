"""
serenity/background.py
run_background_ingest
（原 server.py 3073-3099 行）
"""
import subprocess
import sys
import time

from .config import ROOT
from .services.signal import snapshot_signals
from .services.chat import decay_memories


def run_background_ingest():
    # Wait 10 seconds after server starts
    time.sleep(10)
    while True:
        try:
            print("[Scheduler] Starting automatic incremental price and X fetch...")
            interpreter = sys.executable or "python"
            script_path = ROOT / "scripts" / "ingest.py"
            res = subprocess.run([interpreter, str(script_path), "prices"], capture_output=True, text=True)
            if res.returncode == 0:
                print("[Scheduler] Automatic incremental price update completed successfully.")
            else:
                print(f"[Scheduler] Price update returned code {res.returncode}. Stderr: {res.stderr.strip()}")

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
