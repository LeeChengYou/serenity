"""
serenity/app.py
main()：argparse（--host/--port/--snapshot-once 不變）、組裝啟動
（原 server.py 3102-3129 行）
"""
import argparse
import sys
import threading
from http.server import ThreadingHTTPServer

from .config import ROOT, get_setting
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

    # fail-secure：非 localhost 且未設 auth_token → 拒絕啟動
    _local_hosts = {"127.0.0.1", "localhost"}
    if args.host not in _local_hosts and not get_setting("auth_token"):
        print(
            "錯誤：以非本機位址（非 127.0.0.1/localhost）啟動時，必須先設定存取 token。\n"
            "請透過設定視窗或環境變數 SERENITY_AUTH_TOKEN 設定 token 後再啟動。\n"
            "範例：SERENITY_AUTH_TOKEN=your_secret_token python scripts/server.py --host 0.0.0.0",
            file=sys.stderr,
        )
        sys.exit(2)

    # Start background scheduler thread
    t = threading.Thread(target=run_background_ingest, daemon=True)
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}")
    server.serve_forever()
