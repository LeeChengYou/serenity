#!/usr/bin/env python3
import argparse
import datetime as dt
import html
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"
RAW_DIR = ROOT / "data" / "raw"
TARGET_USER_ID = "1940360837547565056"
X_CURL_DIR = ROOT / "x_curl"
CURL_FILES = {
    "posts": "UserTweets.curl",
    "replies": "UserTweetsAndReplies.curl",
    "premium": "UserSuperFollowTweets.curl",
}
CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$([A-Z][A-Z0-9.]{0,9})(?![A-Za-z0-9_])")
NOISE_SYMBOLS = {"AI", "I", "A", "USD", "US", "CEO", "ETF", "IPO"}


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        pragma journal_mode = wal;
        create table if not exists raw_pages (
            id integer primary key autoincrement,
            source text not null,
            cursor text,
            fetched_at text not null,
            body text not null,
            unique(source, cursor)
        );
        create table if not exists tweets (
            tweet_id text primary key,
            source text not null,
            author_id text,
            author_screen_name text,
            created_at text,
            text text not null,
            url text,
            favorite_count integer,
            reply_count integer,
            retweet_count integer,
            quote_count integer,
            raw_json text not null
        );
        create table if not exists mentions (
            id integer primary key autoincrement,
            symbol text not null,
            tweet_id text not null references tweets(tweet_id) on delete cascade,
            mentioned_at text not null,
            text text not null,
            source text not null,
            unique(symbol, tweet_id)
        );
        create table if not exists prices (
            symbol text not null,
            date text not null,
            open real,
            high real,
            low real,
            close real not null,
            volume integer,
            primary key(symbol, date)
        );
        create table if not exists news_sentiment (
            id              integer primary key autoincrement,
            symbol          text not null,
            source          text not null,
            published_at    text not null,
            headline        text,
            sentiment       text,
            sentiment_score real,
            url             text,
            content_snippet text
        );
        create index if not exists idx_mentions_symbol_time on mentions(symbol, mentioned_at);
        create index if not exists idx_prices_symbol_date on prices(symbol, date);
        create index if not exists idx_news_sentiment_symbol on news_sentiment(symbol, published_at);
        """
    )
    migrate_prices_ohlc(con)
    migrate_news_sentiment(con)
    return con


def migrate_prices_ohlc(con):
    """
    Idempotent migration: add open/high/low columns to an existing prices
    table that was created before OHLC support.  Safe to call on a fresh DB
    (columns already present) or on a legacy DB (columns missing).
    """
    existing = {row[1] for row in con.execute("PRAGMA table_info(prices)")}
    for col in ("open", "high", "low"):
        if col not in existing:
            con.execute(f"ALTER TABLE prices ADD COLUMN {col} REAL")
            print(f"[migrate] Added column prices.{col}")
    con.commit()


def migrate_news_sentiment(con):
    """
    Idempotent migration: ensure the news_sentiment table exists.
    The CREATE TABLE in connect() already handles fresh DBs; this covers
    any edge-case where the table was created without the index.
    """
    con.execute("""
        create table if not exists news_sentiment (
            id              integer primary key autoincrement,
            symbol          text not null,
            source          text not null,
            published_at    text not null,
            headline        text,
            sentiment       text,
            sentiment_score real,
            url             text,
            content_snippet text
        )
    """)
    con.execute("""
        create index if not exists idx_news_sentiment_symbol
            on news_sentiment(symbol, published_at)
    """)
    con.commit()


def parse_curl(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    # Normalize Windows cmd style cURL by stripping carets
    text = text.replace('^', '')
    args = [arg for arg in shlex.split(text, posix=True) if arg.strip()]
    if not args or args[0] != "curl":
        raise ValueError(f"{path} is not a curl command")
    return args


def set_cursor(url: str, cursor: str | None) -> str:
    parts = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    variables = json.loads(qs.get("variables", ["{}"])[0])
    if cursor:
        variables["cursor"] = cursor
    else:
        variables.pop("cursor", None)
    qs["variables"] = [json.dumps(variables, separators=(",", ":"))]
    query = urllib.parse.urlencode(qs, doseq=True)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def curl_fetch(curl_file: Path, cursor: str | None):
    args = parse_curl(curl_file)
    args[1] = set_cursor(args[1], cursor)
    args.extend(["-sS", "--compressed"])
    out = subprocess.check_output(args, cwd=ROOT)
    body = out.decode("utf-8", "replace")
    data = json.loads(body)
    if "errors" in data and not data.get("data"):
        raise RuntimeError(json.dumps(data["errors"], ensure_ascii=False)[:1000])
    return body, data


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for val in obj.values():
            yield from walk(val)
    elif isinstance(obj, list):
        for val in obj:
            yield from walk(val)


def find_bottom_cursor(data):
    for node in walk(data):
        if node.get("cursorType") == "Bottom" and node.get("value"):
            return node["value"]
    return None


def normalize_tweet(node):
    if node.get("__typename") != "Tweet" or "legacy" not in node:
        return None
    legacy = node.get("legacy", {})
    core_user = (((node.get("core") or {}).get("user_results") or {}).get("result") or {})
    author_id = core_user.get("rest_id") or legacy.get("user_id_str")
    if author_id != TARGET_USER_ID:
        return None
    tweet_id = legacy.get("id_str") or node.get("rest_id")
    if not tweet_id:
        return None
    note = (((node.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    text = note.get("text") or legacy.get("full_text") or ""
    text = html.unescape(text)
    created_at = parse_x_date(legacy.get("created_at"))
    screen = (((core_user.get("core") or {}).get("screen_name")) or "aleabitoreddit")
    return {
        "tweet_id": tweet_id,
        "author_id": author_id,
        "author_screen_name": screen,
        "created_at": created_at,
        "text": text,
        "url": f"https://x.com/{screen}/status/{tweet_id}",
        "favorite_count": legacy.get("favorite_count") or 0,
        "reply_count": legacy.get("reply_count") or 0,
        "retweet_count": legacy.get("retweet_count") or 0,
        "quote_count": legacy.get("quote_count") or 0,
        "symbols": extract_symbols(text, legacy, note),
        "raw_json": json.dumps(node, ensure_ascii=False, separators=(",", ":")),
    }


def parse_x_date(value):
    if not value:
        return None
    parsed = dt.datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def extract_symbols(text, legacy, note):
    found = set()
    for m in CASHTAG_RE.finditer(text or ""):
        found.add(m.group(1).upper())
    entity_sets = [legacy.get("entities") or {}, note.get("entity_set") or {}]
    for entities in entity_sets:
        for item in entities.get("symbols") or []:
            symbol = item.get("text") or (((item.get("tag") or {}).get("info") or {}).get("info") or {}).get("ticker")
            if symbol:
                found.add(symbol.upper())
    cleaned = set()
    for s in found:
        s = s.upper().strip()
        if s.endswith(".") and s.count(".") == 1:
            s = s[:-1]
        cleaned.add(s)
    return sorted(s for s in cleaned if s not in NOISE_SYMBOLS and 1 < len(s) <= 10)


def ingest_page(con, source, body, data, cursor):
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            "insert or ignore into raw_pages(source, cursor, fetched_at, body) values (?, ?, ?, ?)",
            (source, cursor or "", now, body),
        )
        tweets = {}
        for node in walk(data):
            t = normalize_tweet(node)
            if t:
                tweets[t["tweet_id"]] = t
        for t in tweets.values():
            con.execute(
                """insert into tweets(tweet_id, source, author_id, author_screen_name, created_at, text, url,
                       favorite_count, reply_count, retweet_count, quote_count, raw_json)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict(tweet_id) do update set
                       source=excluded.source, created_at=excluded.created_at, text=excluded.text, url=excluded.url,
                       favorite_count=excluded.favorite_count, reply_count=excluded.reply_count,
                       retweet_count=excluded.retweet_count, quote_count=excluded.quote_count, raw_json=excluded.raw_json""",
                (t["tweet_id"], source, t["author_id"], t["author_screen_name"], t["created_at"], t["text"], t["url"],
                 t["favorite_count"], t["reply_count"], t["retweet_count"], t["quote_count"], t["raw_json"]),
            )
            for symbol in t["symbols"]:
                con.execute(
                    "insert or ignore into mentions(symbol, tweet_id, mentioned_at, text, source) values (?, ?, ?, ?, ?)",
                    (symbol, t["tweet_id"], t["created_at"], t["text"], source),
                )
        con.execute("COMMIT")
        return len(tweets)
    except Exception:
        con.execute("ROLLBACK")
        raise


def fetch_x(max_pages=20, pause=0.8):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        total = 0
        for source, filename in CURL_FILES.items():
            cursor = None
            seen = set()
            for page in range(max_pages):
                if cursor in seen:
                    break
                seen.add(cursor)
                print(f"fetch {source} page={page + 1} cursor={'initial' if not cursor else cursor[:18]}")
                try:
                    body, data = curl_fetch(X_CURL_DIR / filename, cursor)
                except Exception as exc:
                    print(f"  stop {source}: {exc}", file=sys.stderr)
                    break
                raw_path = RAW_DIR / f"{source}_{page + 1}.json"
                raw_path.write_text(body, encoding="utf-8")
                n = ingest_page(con, source, body, data, cursor)
                total += n
                next_cursor = find_bottom_cursor(data)
                if not next_cursor or next_cursor == cursor or n == 0:
                    break
                cursor = next_cursor
                time.sleep(pause)
        print(f"saved/updated {total} tweet rows into {DB_PATH}")
    finally:
        con.close()


def symbol_list(con, min_mentions=2):
    rows = con.execute("""
        select symbol from mentions
        group by symbol
        having count(*) >= ?
        order by count(*) desc, symbol
    """, (min_mentions,)).fetchall()
    return [r[0] for r in rows]


def yahoo_chart(symbol, start, end, max_retries=3):
    period1 = int(start.timestamp())
    period2 = int(end.timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?period1={period1}&period2={period2}&interval=1d&events=history"
    
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"  [WARN] {symbol}: Yahoo 429 Rate Limited. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  [WARN] {symbol}: Network error/timeout ({e}). Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise


def fetch_prices(days_back=420, min_mentions=2):
    con = connect()
    try:
        symbols = symbol_list(con, min_mentions)
        if not symbols:
            print("no symbols yet; run fetch-x first")
            return
        today = dt.datetime.now(dt.timezone.utc)
        for symbol in symbols:
            try:
                # Query the latest date in DB for this symbol
                row = con.execute("select max(date) from prices where symbol=?", (symbol,)).fetchone()
                latest_date_str = row[0] if row else None

                # Is OHLC already populated for the latest bar?  Older rows may
                # predate OHLC support (open/high/low NULL); those need a full
                # re-fetch to backfill, so we must not take the incremental skip.
                ohlc_ready = False
                if latest_date_str:
                    orow = con.execute(
                        "select open from prices where symbol=? and date=?",
                        (symbol, latest_date_str),
                    ).fetchone()
                    ohlc_ready = orow is not None and orow[0] is not None

                # Determine start date (incremental — preserve WIP behaviour)
                if latest_date_str and ohlc_ready:
                    latest_dt = dt.datetime.strptime(latest_date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
                    days_diff = (today - latest_dt).days
                    if days_diff <= 1:
                        print(f"price {symbol} - already up to date (latest: {latest_date_str})")
                        continue
                    fetch_start = latest_dt + dt.timedelta(days=1)
                    print(f"price {symbol} - incremental from {fetch_start.date()} (latest: {latest_date_str})")
                else:
                    fetch_start = today - dt.timedelta(days=days_back)
                    reason = "backfill OHLC" if latest_date_str else "first fetch"
                    print(f"price {symbol} - full fetch ({reason}) from {fetch_start.date()}")

                data = yahoo_chart(symbol, fetch_start, today + dt.timedelta(days=2))
                result = (data.get("chart") or {}).get("result") or []
                if not result:
                    print(f"  no yahoo result for {symbol}")
                    continue
                res = result[0]
                timestamps = res.get("timestamp") or []
                quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
                opens   = quote.get("open")   or []
                highs   = quote.get("high")   or []
                lows    = quote.get("low")    or []
                closes  = quote.get("close")  or []
                volumes = quote.get("volume") or []

                def _guard(val):
                    """Return float if finite and within a sane price range, else None."""
                    if val is None:
                        return None
                    try:
                        f = float(val)
                        if not math.isfinite(f) or f < 0 or f > 100000:
                            return None
                        return f
                    except (TypeError, ValueError):
                        return None

                inserted = 0
                for ts, open_v, high_v, low_v, close_v, vol in zip(timestamps, opens, highs, lows, closes, volumes):
                    close_f = _guard(close_v)
                    if close_f is None or close_f <= 0:
                        continue  # skip bars with bad close — never fabricate
                    open_f = _guard(open_v)
                    high_f = _guard(high_v)
                    low_f = _guard(low_v)
                    date = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
                    con.execute(
                        """insert or replace into prices(symbol, date, open, high, low, close, volume)
                           values (?, ?, ?, ?, ?, ?, ?)""",
                        (symbol, date, open_f, high_f, low_f, close_f, int(vol or 0) if vol is not None else None),
                    )
                    inserted += 1
                con.commit()
                print(f"  {inserted} bars")
                time.sleep(0.2)
            except Exception as exc:
                print(f"  failed {symbol}: {exc}", file=sys.stderr)
    finally:
        con.close()


def fetch_stocktwits(symbol: str, con=None, limit: int = 30) -> int:
    """
    Fetch up to `limit` recent messages for `symbol` from the public
    StockTwits stream API and store them in the `news_sentiment` table.

    Returns the number of new rows inserted.  Skips messages that are
    missing a timestamp or body.  Skips rows already present (insert or
    ignore on unique constraint).

    No authentication is required for the public stream endpoint:
      GET https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json

    Sentiment values reported by StockTwits are 'Bullish' or 'Bearish';
    messages with no sentiment are stored as 'Neutral'.

    Rate limits: the endpoint allows roughly 200 requests/hour
    unauthenticated.  Callers should add a pause between symbols when
    processing a batch.  On HTTP 429 or any network error this function
    logs the error and returns 0 — it never fabricates data.
    """
    own_connection = con is None
    if own_connection:
        con = connect()

    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        print(f"  stocktwits {symbol}: HTTP {exc.code} — skipping", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"  stocktwits {symbol}: {exc} — skipping", file=sys.stderr)
        return 0

    try:
        data = json.loads(body)
    except Exception as exc:
        print(f"  stocktwits {symbol}: JSON parse error {exc}", file=sys.stderr)
        return 0

    messages = (data.get("messages") or [])[:limit]
    inserted = 0
    for msg in messages:
        msg_id = msg.get("id")
        created_at = msg.get("created_at")
        body_text = (msg.get("body") or "").strip()
        if not created_at or not body_text:
            continue  # skip malformed messages — never fake

        # Normalise timestamp to ISO-8601 UTC
        try:
            ts = dt.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
            ts = ts.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            ts = created_at  # keep as-is if format unexpected

        # Parse sentiment field (may be absent)
        entities = msg.get("entities") or {}
        sentiment_obj = entities.get("sentiment") or {}
        sentiment_basic = (sentiment_obj.get("basic") or "").strip() or "Neutral"

        msg_url = f"https://stocktwits.com/message/{msg_id}" if msg_id else None

        try:
            con.execute(
                """insert or ignore into news_sentiment
                   (symbol, source, published_at, headline, sentiment,
                    sentiment_score, url, content_snippet)
                   values (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    symbol.upper(),
                    "stocktwits",
                    ts,
                    body_text[:500],
                    sentiment_basic,
                    None,  # StockTwits public API does not expose numeric scores
                    msg_url,
                    None,
                ),
            )
            inserted += con.execute("select changes()").fetchone()[0]
        except sqlite3.Error as exc:
            print(f"  stocktwits insert error ({symbol} msg {msg_id}): {exc}", file=sys.stderr)

    if own_connection:
        con.commit()
    else:
        con.commit()

    print(f"  stocktwits {symbol}: {inserted} new rows (of {len(messages)} fetched)")
    return inserted


def fetch_stocktwits_all(min_mentions: int = 2, pause: float = 1.0) -> None:
    """
    Fetch StockTwits data for every tracked symbol (ordered by mention count).
    Pauses `pause` seconds between requests to stay within rate limits.
    """
    con = connect()
    symbols = symbol_list(con, min_mentions)
    if not symbols:
        print("no symbols yet; run fetch-x first")
        return
    total = 0
    for symbol in symbols:
        total += fetch_stocktwits(symbol, con=con)
        time.sleep(pause)
    print(f"stocktwits: {total} total new rows for {len(symbols)} symbols")


def main():
    ap = argparse.ArgumentParser(description="Ingest Serenity X posts, symbols and Yahoo prices into SQLite.")
    ap.add_argument("command", choices=["fetch-x", "prices", "all", "stats", "stocktwits"])
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--days", type=int, default=420)
    ap.add_argument("--min-mentions", type=int, default=2)
    ap.add_argument("--symbol", help="Single symbol for stocktwits subcommand")
    ap.add_argument("--pause", type=float, default=1.0, help="Seconds between StockTwits requests")
    args = ap.parse_args()
    if args.command in {"fetch-x", "all"}:
        fetch_x(args.max_pages)
    if args.command in {"prices", "all"}:
        fetch_prices(args.days, args.min_mentions)
    if args.command in {"stocktwits", "all"}:
        if args.symbol:
            con = connect()
            fetch_stocktwits(args.symbol.upper(), con=con)
            con.commit()
        else:
            fetch_stocktwits_all(args.min_mentions, args.pause)
    if args.command == "stats":
        con = connect()
        try:
            print("tweets", con.execute("select count(*) from tweets").fetchone()[0])
            print("mentions", con.execute("select count(*) from mentions").fetchone()[0])
            print("prices", con.execute("select count(*) from prices").fetchone()[0])
            print("news_sentiment", con.execute("select count(*) from news_sentiment").fetchone()[0])
            for row in con.execute("select symbol, count(*) c, min(mentioned_at), max(mentioned_at) from mentions group by symbol order by c desc, symbol"):
                print(dict(row))
        finally:
            con.close()


if __name__ == "__main__":
    main()
