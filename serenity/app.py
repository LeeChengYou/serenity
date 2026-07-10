"""
serenity/app.py
main()：argparse（--host/--port/--snapshot-once 不變）、組裝啟動
（原 server.py 3102-3129 行）
"""
import argparse
import threading
from http.server import ThreadingHTTPServer

from .config import ROOT
from .api.handler import Handler
from .background import run_background_ingest
from .services.signal import snapshot_signals


def main():
    ap = argparse.ArgumentParser(description="Serve the Serenity dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument(
        "--snapshot-once",
        action="store_true",
        help="R-5: Run signal_history snapshot for all priced symbols and exit (no server start).",
    )
    args = ap.parse_args()

    # R-5 CLI hook: run snapshot and exit immediately (no server start)
    if args.snapshot_once:
        count = snapshot_signals()
        print(f"[snapshot-once] Done — {count} rows upserted.")
        return

    # Start background scheduler thread
    t = threading.Thread(target=run_background_ingest, daemon=True)
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}")
    server.serve_forever()
