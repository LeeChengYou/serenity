"""
serenity/services/experts.py
_expert_views_row_to_item, expert_views_payload, expert_views_all_payload
（原 server.py 2259-2334 行）
"""
from datetime import datetime

from ..db import _table_exists


def _expert_views_row_to_item(r) -> dict:
    return {
        "source":       r["source"],
        "author":       r["author"],
        "title":        r["title"],
        "text":         r["text"],
        "url":          r["url"],
        "published_at": r["published_at"],
        "credibility":  r["credibility"],
    }


def expert_views_payload(con, symbol: str) -> dict:
    """
    GET /api/expert-views/<SYM>
    Returns up to 10 items mentioning this symbol, newest first.
    Returns empty structure when table absent (graceful degradation).
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    empty = {"symbol": symbol, "items": [], "as_of": as_of}

    if not _table_exists(con, "expert_views"):
        return empty

    try:
        sym_json = symbol  # we search inside the JSON array string
        rows = con.execute(
            """select source, author, title, text, url, published_at, credibility
               from expert_views
               where symbols like ?
                  or symbols like ?
                  or symbols like ?
                  or symbols like ?
               order by published_at desc
               limit 10""",
            (
                f'["{sym_json}"]',
                f'["{sym_json}",%',
                f'%,"{sym_json}",%',
                f'%,"{sym_json}"]',
            ),
        ).fetchall()
        items = [_expert_views_row_to_item(r) for r in rows]
        return {"symbol": symbol, "items": items, "as_of": as_of}
    except Exception as exc:
        print(f"[expert_views_payload] {symbol}: {exc}")
        return empty


def expert_views_all_payload(con) -> dict:
    """
    GET /api/expert-views
    Returns latest 20 items across all symbols, newest first.
    """
    as_of = datetime.now().strftime("%Y-%m-%d")
    empty = {"items": [], "as_of": as_of}

    if not _table_exists(con, "expert_views"):
        return empty

    try:
        rows = con.execute(
            """select source, author, title, text, url, published_at, credibility
               from expert_views
               order by published_at desc
               limit 20"""
        ).fetchall()
        items = [_expert_views_row_to_item(r) for r in rows]
        return {"items": items, "as_of": as_of}
    except Exception as exc:
        print(f"[expert_views_all_payload]: {exc}")
        return empty
