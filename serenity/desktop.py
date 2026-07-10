"""
serenity/desktop.py
桌面殼：背景執行緒啟動 ThreadingHTTPServer，pywebview 原生視窗指向 localhost。

用法：
  python -m serenity.desktop               # 開原生視窗（需 pywebview）
  python -m serenity.desktop --smoke       # headless 自驗 HTTP 200，exit 0
  python -m serenity.desktop --port 8800   # 指定 port（0 = 自動挑空閒）

支援 python -m serenity.desktop（模組入口點）。
"""
import argparse
import socket
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from .api.handler import Handler
from .background import run_background_ingest


def _pick_free_port() -> int:
    """讓 OS 自動挑一個可用 port。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int) -> ThreadingHTTPServer:
    """在背景執行緒啟動 ThreadingHTTPServer，回傳 server 物件。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    # 背景排程（prices ingest + snapshot + decay）
    bg = threading.Thread(target=run_background_ingest, daemon=True)
    bg.start()

    # HTTP server 自身也用 daemon 執行緒，關窗後隨進程結束
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    return server


def main():
    ap = argparse.ArgumentParser(description="Serenity 桌面應用")
    ap.add_argument("--port", type=int, default=0, help="監聽 port（0=自動挑空閒）")
    ap.add_argument("--smoke", action="store_true", help="headless 自驗模式：起 server→驗 HTTP 200→exit 0")
    args = ap.parse_args()

    port = args.port if args.port != 0 else _pick_free_port()
    url = f"http://127.0.0.1:{port}/index.html"

    # ── 啟動 server ──────────────────────────────────────────────────────────
    server = _start_server(port)

    if args.smoke:
        # headless 模式：不 import webview，自打 HTTP 驗 200 後退出
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                if resp.status == 200:
                    print("[smoke] OK")
                    server.shutdown()
                    sys.exit(0)
                else:
                    print(f"[smoke] FAIL: HTTP {resp.status}")
                    server.shutdown()
                    sys.exit(1)
        except Exception as e:
            print(f"[smoke] FAIL: {e}")
            server.shutdown()
            sys.exit(1)
    else:
        # 正常模式：開 pywebview 原生視窗
        try:
            import webview  # 函式內 import，未安裝不炸
        except ImportError:
            print(
                "錯誤：pywebview 未安裝。\n"
                "請執行：pip install pywebview\n"
                "安裝後重新啟動本程式。"
            )
            server.shutdown()
            sys.exit(1)

        win = webview.create_window("Serenity", url)
        webview.start()
        # 關窗後 server 隨進程結束（daemon thread）


if __name__ == "__main__":
    main()
