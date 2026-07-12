# -*- coding: utf-8 -*-
"""
(e) 新聞·專家獨立頁 驗收測試（規格：docs/REQUIREMENTS_AI_MARKET.md e-驗收 1-6）

執行：PYTHONIOENCODING=utf-8 python scratch/test_news_page.py
通過標準：0 failed、exit 0。零外網、零真實 DB 寫入。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SERENITY_NO_DOTENV", "1")

_MAIN_DB = r"C:\Users\Jeff\OneDrive\桌面\git_repo\serenity\data\serenity.sqlite"
_DB_SRC = os.environ.get("SERENITY_DB_SRC", _MAIN_DB)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    mark = "  OK " if ok else "! FAIL"
    print(f"{mark}  {name}" + (f" -- {detail}" if (detail and not ok) else ""))
    return ok


def finish():
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print()
    print("=" * 70)
    print(f"News Page Acceptance — {passed} passed / {failed} failed")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)


# ---------------------------------------------------------------------------
# tempfile DB with news table
# ---------------------------------------------------------------------------

def _make_temp_db() -> tuple[str, sqlite3.Connection]:
    """
    Fresh empty tempfile DB with minimal schema for news tests.
    We intentionally do NOT copy the real DB — news tests need a clean slate
    to count rows and assert titles precisely.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    import ingest
    original_db_path = ingest.DB_PATH
    ingest.DB_PATH = Path(tmp.name)
    con = ingest.connect()
    ingest.migrate_news(con)
    ingest.DB_PATH = original_db_path
    return tmp.name, con


def _news_route(db_path: str, path: str, q_str: str = ""):
    """Call handler.route_api with a fresh connection (route_api closes it)."""
    from serenity.api.handler import Handler
    import serenity.api.handler as hmod
    h = Handler.__new__(Handler)
    qd = parse_qs(q_str)
    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    with mock.patch.object(hmod, "DB_PATH", Path(db_path)), \
         mock.patch.object(hmod, "db", return_value=fresh):
        return h.route_api(path, qd)


def _insert_news(con, rows: list[dict]):
    """Bulk-insert news rows. Each dict: title, source, url, published_at, scope, symbols, summary."""
    for r in rows:
        con.execute(
            "INSERT OR IGNORE INTO news(title, source, url, published_at, scope, symbols, summary, fetched_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, '2026-07-12T00:00:00Z')",
            (r["title"], r.get("source", "test"), r["url"], r["published_at"],
             r.get("scope", "macro"), r.get("symbols"), r.get("summary")),
        )
    con.commit()


# ---------------------------------------------------------------------------
# e-驗收 1: 預設 limit 50；limit>200 被夾到 200
# ---------------------------------------------------------------------------

def test_limit_default_and_cap():
    print("\n--- e-驗收 1: limit 預設 50、上限 200 ---")
    tmp_name, con = _make_temp_db()
    try:
        # 插入 210 筆
        rows = [
            {
                "title": f"News {i}",
                "url": f"http://example.com/{i}",
                "published_at": f"2026-06-{i % 28 + 1:02d}T12:00:00Z",
                "scope": "macro",
            }
            for i in range(210)
        ]
        _insert_news(con, rows)
        con.close()

        # 預設 limit（不帶 limit 參數）
        res = _news_route(tmp_name, "/api/news-feed")
        check("預設 limit 50：items 數量 ≤50", len(res["items"]) <= 50,
              f"got {len(res['items'])}")
        check("預設 limit 50：items 數量 =50（有 210 筆）", len(res["items"]) == 50,
              f"got {len(res['items'])}")

        # limit=300 → 夾到 200
        res = _news_route(tmp_name, "/api/news-feed", "limit=300")
        check("limit=300 被夾到 200", len(res["items"]) == 200,
              f"got {len(res['items'])}")

        # limit=abc → 不炸，預設 50
        res = _news_route(tmp_name, "/api/news-feed", "limit=abc")
        check("limit=abc 不炸，回 50 筆", len(res["items"]) == 50,
              f"got {len(res['items'])}")

        # limit=-5 → max(1,...) → 夾到 1
        res = _news_route(tmp_name, "/api/news-feed", "limit=-5")
        check("limit=-5 不炸，回 ≥1 筆", len(res["items"]) >= 1,
              f"got {len(res['items'])}")
    finally:
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# e-驗收 2: DESC 排序；symbol 篩選（macro 列不出現）
# ---------------------------------------------------------------------------

def test_sort_and_symbol_filter():
    print("\n--- e-驗收 2: DESC 排序 + symbol 篩選 ---")
    tmp_name, con = _make_temp_db()
    try:
        rows = [
            {"title": "Macro A", "url": "http://e.com/m1",
             "published_at": "2026-06-01T10:00:00Z", "scope": "macro",
             "symbols": "[]"},
            {"title": "NVDA News", "url": "http://e.com/n1",
             "published_at": "2026-06-03T10:00:00Z", "scope": "symbol",
             "symbols": '["NVDA"]'},
            {"title": "AAPL News", "url": "http://e.com/a1",
             "published_at": "2026-06-02T10:00:00Z", "scope": "symbol",
             "symbols": '["AAPL"]'},
        ]
        _insert_news(con, rows)
        con.close()

        # 無篩選 → DESC 順序
        res = _news_route(tmp_name, "/api/news-feed", "limit=10")
        dates = [i["published_at"] for i in res["items"]]
        check("DESC 排序（newest first）", dates == sorted(dates, reverse=True), str(dates))

        # symbol=NVDA → 只回含 NVDA 的列
        res = _news_route(tmp_name, "/api/news-feed", "symbol=NVDA&limit=10")
        titles = [i["title"] for i in res["items"]]
        check("symbol=NVDA 命中 NVDA News", "NVDA News" in titles, str(titles))
        check("symbol=NVDA 不含 AAPL News", "AAPL News" not in titles, str(titles))
        check("symbol=NVDA 不含 macro Macro A（symbols=[]）",
              "Macro A" not in titles, str(titles))
    finally:
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# e-驗收 3: before cursor + has_more 語義
# ---------------------------------------------------------------------------

def test_before_cursor_and_has_more():
    print("\n--- e-驗收 3: before cursor + has_more ---")
    tmp_name, con = _make_temp_db()
    try:
        # 5 筆，日期嚴格遞增
        rows = [
            {"title": f"News {i}", "url": f"http://e.com/c{i}",
             "published_at": f"2026-06-{i:02d}T12:00:00Z", "scope": "macro"}
            for i in range(1, 6)
        ]
        _insert_news(con, rows)
        con.close()

        # 第一頁 limit=2 → has_more=True
        res = _news_route(tmp_name, "/api/news-feed", "limit=2")
        check("第一頁 has_more=True（5>2）", res["has_more"] is True, str(res["has_more"]))
        cursor = res["items"][-1]["published_at"]

        # 第二頁 before=cursor → 全部 published_at < cursor
        res2 = _news_route(tmp_name, "/api/news-feed",
                           f"limit=10&before={cursor}")
        dates2 = [i["published_at"] for i in res2["items"]]
        check("第二頁全部 published_at < cursor",
              all(d < cursor for d in dates2), f"cursor={cursor} dates={dates2}")

        # 取盡 → has_more=False
        res3 = _news_route(tmp_name, "/api/news-feed", "limit=200")
        check("取盡 has_more=False", res3["has_more"] is False, str(res3["has_more"]))
    finally:
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# e-驗收 4: 壞 symbols JSON 列不炸，symbols=[]
# ---------------------------------------------------------------------------

def test_bad_symbols_json():
    print("\n--- e-驗收 4: 壞 symbols JSON 不炸 ---")
    tmp_name, con = _make_temp_db()
    try:
        con.execute(
            "INSERT OR IGNORE INTO news(title, source, url, published_at, scope, symbols, fetched_at)"
            " VALUES('Bad JSON', 'test', 'http://e.com/bad1',"
            "  '2026-06-01T00:00:00Z', 'macro', '{broken json}', '2026-07-12T00:00:00Z')"
        )
        con.execute(
            "INSERT OR IGNORE INTO news(title, source, url, published_at, scope, symbols, fetched_at)"
            " VALUES('Null Sym', 'test', 'http://e.com/bad2',"
            "  '2026-06-02T00:00:00Z', 'macro', NULL, '2026-07-12T00:00:00Z')"
        )
        con.commit()
        con.close()

        res = _news_route(tmp_name, "/api/news-feed", "limit=10")
        bad1 = next((i for i in res["items"] if i["title"] == "Bad JSON"), None)
        bad2 = next((i for i in res["items"] if i["title"] == "Null Sym"), None)
        check("壞 JSON 列不炸，API 正常回傳", res is not None and "items" in res)
        check("壞 JSON 列 symbols=[]", bad1 is not None and bad1["symbols"] == [],
              str(bad1))
        check("NULL symbols 列 symbols=[]", bad2 is not None and bad2["symbols"] == [],
              str(bad2))
    finally:
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# e-驗收 5: /api/expert-views 迴歸（回 items 鍵）
# ---------------------------------------------------------------------------

def test_expert_views_regression():
    print("\n--- e-驗收 5: /api/expert-views 迴歸 ---")
    tmp_name, con = _make_temp_db()
    try:
        # 確保 expert_views 表存在（db() 初始化會建，但我們直接用 ingest con）
        con.execute("""
            create table if not exists expert_views (
                id           integer primary key autoincrement,
                source       text not null,
                author       text,
                title        text,
                text         text not null,
                url          text unique not null,
                published_at text,
                symbols      text,
                credibility  text not null default 'individual',
                fetched_at   text not null
            )
        """)
        con.execute(
            "INSERT OR IGNORE INTO expert_views(source, author, title, text, url, published_at, credibility, fetched_at)"
            " VALUES('Cathie Wood', 'ARK', 'AI outlook', 'Very bullish on AI',"
            "  'http://ev.test/1', '2026-06-01T00:00:00Z', 0.9, '2026-07-12T00:00:00Z')"
        )
        con.commit()

        from serenity.services.experts import expert_views_all_payload
        res = expert_views_all_payload(con)

        check("回傳有 items 鍵", "items" in res, str(list(res.keys())))
        check("items 是 list", isinstance(res["items"], list), type(res["items"]).__name__)
        check("items 至少 1 筆", len(res["items"]) >= 1, f"got {len(res['items'])}")
        item = res["items"][0] if res["items"] else {}
        check("item 有 source 欄", "source" in item, str(list(item.keys())))
        check("item 有 credibility 欄", "credibility" in item, str(list(item.keys())))
        check("item 沒有 symbols 欄（_expert_views_row_to_item 不含）",
              "symbols" not in item, str(list(item.keys())))
        check("回傳有 as_of 鍵", "as_of" in res)
        # 確認沒有 views / expert_views 舊鍵
        check("沒有舊 views 鍵", "views" not in res, str(list(res.keys())))
        check("沒有舊 expert_views 鍵", "expert_views" not in res, str(list(res.keys())))
    finally:
        con.close()
        try:
            Path(tmp_name).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_limit_default_and_cap()
    test_sort_and_symbol_filter()
    test_before_cursor_and_has_more()
    test_bad_symbols_json()
    test_expert_views_regression()
    finish()
