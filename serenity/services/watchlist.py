"""
serenity/services/watchlist.py
watchlist CRUD API handlers
GET /api/watchlist  → {"symbols":[{"symbol","added_at","mentions":int}...]}
POST /api/watchlist {"add": sym} | {"remove": sym}  → 200 + updated list / 400
"""
import re
import threading
from datetime import datetime


_SYM_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")


def _watchlist_payload(con) -> dict:
    """Build the watchlist payload with mention counts."""
    rows = con.execute(
        "select symbol, added_at from watchlist order by added_at, symbol"
    ).fetchall()
    result = []
    for r in rows:
        sym = r[0]
        added_at = r[1]
        try:
            cnt = con.execute(
                "select count(*) from mentions where symbol=?", (sym,)
            ).fetchone()[0]
        except Exception:
            cnt = 0
        result.append({"symbol": sym, "added_at": added_at, "mentions": cnt})
    return {"symbols": result}


def handle_get_watchlist(con) -> dict:
    """GET /api/watchlist"""
    return _watchlist_payload(con)


def handle_post_watchlist(con, payload: dict) -> tuple:
    """
    POST /api/watchlist
    Returns (response_dict, status_code).
    Raises ValueError for bad input (→ caller maps to 400).
    """
    if "add" in payload:
        raw = str(payload["add"]).strip()
        if not _SYM_RE.match(raw):
            raise ValueError(f"代號格式錯誤：{raw!r}（允許 ^[A-Za-z0-9.\\-]{{1,12}}$）")
        sym = raw.upper()
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "insert or ignore into watchlist(symbol, added_at) values (?, ?)",
            (sym, now),
        )
        con.commit()
        # 新增成功後，開 daemon thread 補抓價格（失敗只 print 不炸）
        def _fetch():
            try:
                import sys
                from pathlib import Path
                _root = Path(__file__).resolve().parents[3]
                if str(_root) not in sys.path:
                    sys.path.insert(0, str(_root))
                import scripts.ingest as ingest
                _con2 = ingest.connect()
                try:
                    ingest.fetch_prices_for_symbol(_con2, sym)
                except AttributeError:
                    # fetch_prices_for_symbol 不存在則呼叫 fetch_prices（全量，也 OK）
                    pass
                finally:
                    _con2.close()
            except Exception as exc:
                print(f"[watchlist add] background price fetch failed for {sym}: {exc}")
        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        return _watchlist_payload(con), 200

    elif "remove" in payload:
        raw = str(payload["remove"]).strip()
        sym = raw.upper()
        con.execute("delete from watchlist where symbol=?", (sym,))
        con.commit()
        return _watchlist_payload(con), 200

    else:
        raise ValueError("body 必須含 'add' 或 'remove' 欄位")
