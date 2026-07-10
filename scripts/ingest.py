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
import xml.etree.ElementTree as ET
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

# ---------------------------------------------------------------------------
# Benchmark symbols — shared constant (R3-2)
# These are market indices used for regime gauge, NOT stocks.
# They must be excluded from all universe queries (signals, snapshots, hit-rate,
# /api/symbols listing) so they never contaminate universe medians.
# ---------------------------------------------------------------------------
BENCHMARK_SYMBOLS: set[str] = {"SPY", "SOXX", "QQQ"}


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
    migrate_news(con)
    migrate_fundamentals(con)
    migrate_analyst_estimates(con)
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


def migrate_news(con):
    """
    Idempotent migration: create news table (R2-3).
    url is the unique key — re-runs skip existing rows.
    scope: 'symbol' | 'macro'
    symbols: JSON array of tickers associated with this article (may be empty for macro)
    """
    con.execute("""
        create table if not exists news (
            id           integer primary key autoincrement,
            title        text not null,
            source       text,
            url          text unique not null,
            published_at text,
            scope        text not null default 'macro',
            symbols      text,
            summary      text,
            fetched_at   text not null
        )
    """)
    con.execute("""
        create index if not exists idx_news_published_at on news(published_at desc)
    """)
    con.execute("""
        create index if not exists idx_news_scope on news(scope)
    """)
    con.commit()


def migrate_fundamentals(con):
    """
    Idempotent migration: create fundamentals table (R2-3 / C-3).
    symbol is the primary key; all financial fields may be NULL.
    """
    con.execute("""
        create table if not exists fundamentals (
            symbol               text primary key,
            pe                   real,
            forward_pe           real,
            eps_ttm              real,
            revenue_growth_yoy   real,
            gross_margin         real,
            market_cap           real,
            next_earnings_date   text,
            updated_at           text not null
        )
    """)
    con.commit()


def migrate_analyst_estimates(con):
    """
    Idempotent migration: create analyst_estimates table (R3-3).
    symbol is the primary key; all financial fields may be NULL.
    """
    con.execute("""
        create table if not exists analyst_estimates (
            symbol                   text primary key,
            target_mean              real,
            target_median            real,
            target_high              real,
            target_low               real,
            n_analysts               integer,
            recommendation_key       text,
            recommendation_mean      real,
            eps_estimate_current_q   real,
            eps_estimate_next_q      real,
            eps_estimate_current_y   real,
            up_revisions_30d         integer,
            down_revisions_30d       integer,
            updated_at               text not null
        )
    """)
    con.execute("""
        create index if not exists idx_analyst_estimates_symbol
            on analyst_estimates(symbol)
    """)
    con.commit()


def parse_curl(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    # Normalize Windows cmd style cURL:
    # 1. Normalize line continuations first
    t = text.replace('\\\n', ' ').replace('\\\r\n', ' ')
    
    # 2. Extract and sanitize the -b cookie value to make it posix-compliant
    # Matches -b followed by a quoted value: ^"..." or "..."
    # Using positive lookahead to stop at the next argument or end of string.
    pattern = r'(-b\s+)(\^"|")(.+?)(\^"|")(?=\s+-|\s+https?://|\s*$)'
    
    def sanitize_cookie_value(match):
        prefix = match.group(1)
        val = match.group(3)
        # Strip all caret/backslash double-quote escapings inside the value to get the raw string
        raw_val = val.replace('^', '').replace('\\', '')
        # Escape any double quotes in the raw value
        escaped_val = raw_val.replace('"', '\\"')
        # Wrap the whole cookie value in simple double quotes
        return f'{prefix}"{escaped_val}"'
        
    t = re.sub(pattern, sanitize_cookie_value, t)
    
    # 3. Handle other arguments (like -H ^"Header: Value^" or general caret escaping)
    # Convert ^" to "
    t = t.replace('^"', '"')
    # Strip any other standalone carets
    t = t.replace('^', '')
    
    args = [arg for arg in shlex.split(t, posix=True) if arg.strip()]
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
            curl_file = X_CURL_DIR / filename
            if not curl_file.exists():
                print(f"  skip {source}: {filename} not found")
                continue
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
    """Return tracked symbols ordered by mention count, excluding benchmark indices."""
    rows = con.execute("""
        select symbol from mentions
        group by symbol
        having count(*) >= ?
        order by count(*) desc, symbol
    """, (min_mentions,)).fetchall()
    return [r[0] for r in rows if r[0] not in BENCHMARK_SYMBOLS]


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


def fetch_stocktwits(symbol: str, con=None, limit: int = 30) -> "int | None":
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
    processing a batch.  On network/HTTP failure this function logs the
    error and returns None (0 means fetched OK but no new rows) — it
    never fabricates data.
    """
    own_connection = con is None
    if own_connection:
        con = connect()

    url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
    # Cloudflare rejects bare UAs with 403 (since ~2026-07); full browser
    # headers mostly pass but still 403/503 intermittently, hence retries.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    body = None
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            if exc.code in (403, 429, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            break
        except Exception as exc:
            last_err = str(exc)
            break
    if body is None:
        print(f"  stocktwits {symbol}: {last_err} — skipping", file=sys.stderr)
        return None

    try:
        data = json.loads(body)
    except Exception as exc:
        print(f"  stocktwits {symbol}: JSON parse error {exc}", file=sys.stderr)
        return None

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
    failed = 0
    for symbol in symbols:
        n = fetch_stocktwits(symbol, con=con)
        if n is None:
            failed += 1
        else:
            total += n
        time.sleep(pause)
    print(f"stocktwits: {total} total new rows for {len(symbols)} symbols"
          + (f", {failed} fetch failures" if failed else ""))
    if symbols and failed == len(symbols):
        # every symbol failed (e.g. Cloudflare block) — exit non-zero so
        # schedulers and daily_check.py record this as a broken step
        print("stocktwits: all symbols failed — job failure", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# RSS / News helpers
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace. Returns plain text."""
    if not text:
        return ""
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _parse_rss_date(value: str | None) -> str | None:
    """
    Parse an RFC-2822 date string (as used in RSS pubDate) to ISO-8601 UTC.
    Returns None when parsing fails — never fabricates a date.
    """
    if not value:
        return None
    value = value.strip()
    # Try RFC-2822: "Mon, 05 Jul 2026 07:00:00 GMT"
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%d %b %Y %H:%M:%S %z",
    ):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return None


def _fetch_rss(url: str, timeout: int = 20) -> list[dict]:
    """
    Fetch and parse an RSS feed. Returns a list of item dicts with keys:
    title, link, description, pubDate, source_name.
    Raises on network/parse errors — callers must wrap in try/except.
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SerenityBot/2.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()

    # Google News often returns gzip; urlopen decompresses automatically.
    root = ET.fromstring(raw.decode("utf-8", errors="replace"))

    # Handle both <rss><channel> and Atom <feed> (Google News uses RSS 2.0)
    items = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # RSS 2.0
    for item in root.iter("item"):
        def _text(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""
        items.append({
            "title": _text("title"),
            "link": _text("link"),
            "description": _text("description"),
            "pubDate": _text("pubDate"),
        })

    # Atom fallback
    if not items:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            def _atxt(tag):
                el = entry.find(tag, ns)
                return (el.text or "").strip() if el is not None else ""
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link_href = (link_el.get("href") or "") if link_el is not None else ""
            items.append({
                "title": _atxt("{http://www.w3.org/2005/Atom}title"),
                "link": link_href,
                "description": _atxt("{http://www.w3.org/2005/Atom}summary"),
                "pubDate": _atxt("{http://www.w3.org/2005/Atom}updated"),
            })

    return items


def _insert_news_item(con, title: str, source: str, url: str, published_at,
                      scope: str, symbols: list[str], description: str,
                      fetched_at: str) -> int:
    """
    Insert one news item.  Idempotent: INSERT OR IGNORE on url.
    Returns 1 if inserted, 0 if already present.
    """
    if not url or not title:
        return 0
    summary = _strip_html(description)[:300]
    symbols_json = json.dumps(symbols, ensure_ascii=False) if symbols else "[]"
    try:
        con.execute(
            """insert or ignore into news
               (title, source, url, published_at, scope, symbols, summary, fetched_at)
               values (?, ?, ?, ?, ?, ?, ?, ?)""",
            (title[:500], source, url, published_at, scope, symbols_json, summary, fetched_at),
        )
        return con.execute("select changes()").fetchone()[0]
    except sqlite3.Error as exc:
        print(f"  [news insert error] {exc} — url={url[:80]}", file=sys.stderr)
        return 0


# Macro query specs: (query_string, label)
_MACRO_QUERIES = [
    ("Fed+interest+rates", "cnbc-macro"),
    ("US+China+chips+export+controls", "gnews-macro"),
    ("semiconductor+tariffs", "gnews-macro"),
    ("Taiwan+geopolitics+semiconductor", "gnews-macro"),
]

_MACRO_FEEDS = [
    # CNBC Markets RSS
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC Markets"),
    # CNN Money RSS
    ("http://rss.cnn.com/rss/money_latest.rss", "CNN Money"),
]


def fetch_news(min_mentions: int = 2, top_n: int = 30, pause: float = 1.5) -> None:
    """
    News pipeline (R2-3):
    1. Per-symbol: Google News RSS for top_n symbols by mention count.
    2. Macro: CNBC/CNN RSS feeds + Google News fixed macro queries.
    Idempotent on url.  One dead feed never kills the entire run.
    """
    con = connect()
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    total_inserted = 0

    try:
        # --- Symbol feeds ---
        symbols = symbol_list(con, min_mentions)[:top_n]
        print(f"[news] Fetching symbol feeds for {len(symbols)} symbols…")
        for symbol in symbols:
            query = urllib.parse.quote(f"{symbol} stock")
            url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
            try:
                items = _fetch_rss(url)
            except Exception as exc:
                print(f"  [news] {symbol} RSS failed: {exc}", file=sys.stderr)
                time.sleep(pause)
                continue

            inserted = 0
            for item in items:
                link = item.get("link", "")
                title = item.get("title", "")
                pub = _parse_rss_date(item.get("pubDate"))
                inserted += _insert_news_item(
                    con, title, "Google News", link,
                    pub, "symbol", [symbol],
                    item.get("description", ""), fetched_at,
                )
            con.commit()
            total_inserted += inserted
            print(f"  [news] {symbol}: {len(items)} fetched, {inserted} new rows")
            time.sleep(pause)

        # --- Macro: CNBC / CNN feeds ---
        print("[news] Fetching macro RSS feeds…")
        for feed_url, feed_name in _MACRO_FEEDS:
            try:
                items = _fetch_rss(feed_url)
            except Exception as exc:
                print(f"  [news] {feed_name} RSS failed: {exc}", file=sys.stderr)
                time.sleep(pause)
                continue

            inserted = 0
            for item in items:
                link = item.get("link", "")
                title = item.get("title", "")
                pub = _parse_rss_date(item.get("pubDate"))
                inserted += _insert_news_item(
                    con, title, feed_name, link,
                    pub, "macro", [],
                    item.get("description", ""), fetched_at,
                )
            con.commit()
            total_inserted += inserted
            print(f"  [news] {feed_name}: {len(items)} fetched, {inserted} new rows")
            time.sleep(pause)

        # --- Macro: Google News fixed queries ---
        print("[news] Fetching macro Google News queries…")
        for query_raw, source_label in _MACRO_QUERIES:
            url = f"https://news.google.com/rss/search?q={query_raw}&hl=en-US&gl=US&ceid=US:en"
            try:
                items = _fetch_rss(url)
            except Exception as exc:
                print(f"  [news] macro query '{query_raw}' failed: {exc}", file=sys.stderr)
                time.sleep(pause)
                continue

            inserted = 0
            for item in items:
                link = item.get("link", "")
                title = item.get("title", "")
                pub = _parse_rss_date(item.get("pubDate"))
                inserted += _insert_news_item(
                    con, title, source_label, link,
                    pub, "macro", [],
                    item.get("description", ""), fetched_at,
                )
            con.commit()
            total_inserted += inserted
            print(f"  [news] macro '{query_raw}': {len(items)} fetched, {inserted} new rows")
            time.sleep(pause)

    finally:
        print(f"[news] Done — {total_inserted} total new rows inserted into news table.")
        con.close()


# ---------------------------------------------------------------------------
# Fundamentals helpers
# ---------------------------------------------------------------------------

_YFINANCE_AVAILABLE = None  # cached result


def _yfinance_available() -> bool:
    """Check once whether yfinance is importable."""
    global _YFINANCE_AVAILABLE
    if _YFINANCE_AVAILABLE is None:
        try:
            import yfinance  # noqa: F401
            _YFINANCE_AVAILABLE = True
        except ImportError:
            _YFINANCE_AVAILABLE = False
    return _YFINANCE_AVAILABLE


def _yahoo_quote_summary(symbol: str) -> dict:
    """
    Fetch Yahoo Finance quoteSummary via plain HTTP.

    Tries multiple endpoints / host combos in order:
      1. query2.finance.yahoo.com v10 (often skips crumb requirement)
      2. query1.finance.yahoo.com v10
      3. query1.finance.yahoo.com v7 quote (simpler, fewer fields)

    Returns a merged dict of all module responses, or {} on failure.
    Missing fields are absent (callers must use .get()).
    Raises RuntimeError only when all attempts return 401 — callers handle
    the yfinance fallback.
    """
    modules = "summaryDetail,defaultKeyStatistics,financialData,calendarEvents"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finance.yahoo.com/",
    }

    sym_enc = urllib.parse.quote(symbol)
    candidates = [
        # query2 often bypasses crumb
        (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym_enc}"
            f"?modules={urllib.parse.quote(modules)}",
            "v10-q2",
        ),
        (
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym_enc}"
            f"?modules={urllib.parse.quote(modules)}",
            "v10-q1",
        ),
    ]

    last_exc = None
    for url, label in candidates:
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = (data.get("quoteSummary") or {}).get("result") or []
            if not result:
                return {}
            merged = {}
            for module_data in result:
                merged.update(module_data)
            return merged
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (401, 403):
                continue  # try next candidate
            raise
        except Exception as exc:
            last_exc = exc
            continue

    raise RuntimeError(
        f"Yahoo quoteSummary 401/403 for {symbol} (all endpoints failed): {last_exc}"
    )


def _raw_val(d: dict, *keys):
    """Navigate nested dicts via keys, returning .get('raw') or None."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if isinstance(cur, dict):
        return cur.get("raw")
    if cur is None:
        return None
    return cur


def _yf_fallback(symbol: str) -> dict:
    """Use yfinance package as fallback if installed. Returns flat dict or {}."""
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        info = tk.info or {}
        cal = {}
        try:
            cal = tk.calendar or {}
        except Exception:
            pass
        next_earnings = None
        if cal:
            # Calendar may return DataFrame or dict
            try:
                earnings_dates = cal.get("Earnings Date")
                if earnings_dates and len(earnings_dates) > 0:
                    next_earnings = str(earnings_dates[0])[:10]
            except Exception:
                pass
        return {
            "pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "eps_ttm": info.get("trailingEps"),
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "gross_margin": info.get("grossMargins"),
            "market_cap": info.get("marketCap"),
            "next_earnings_date": next_earnings,
        }
    except Exception as exc:
        print(f"  [fundamentals yfinance fallback] {symbol}: {exc}", file=sys.stderr)
        return {}


def fetch_fundamentals(min_mentions: int = 2, pause: float = 1.0) -> None:
    """
    Fundamentals pipeline (R2-3 / C-3):
    Fetches P/E, forward P/E, EPS, revenue growth, gross margin, market cap,
    next earnings date from Yahoo Finance quoteSummary for all tracked symbols.

    Strategy:
    1. Try plain HTTP (no crumb).
    2. On 401 crumb error, soft-import yfinance if available; else store NULLs.
    3. All missing fields → NULL. Never fabricate values.
    Idempotent upsert per symbol.
    """
    con = connect()
    now_str = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    symbols = symbol_list(con, min_mentions)
    if not symbols:
        print("[fundamentals] No symbols found — run fetch-x first.")
        con.close()
        return

    print(f"[fundamentals] Processing {len(symbols)} symbols…")
    total_upserted = 0

    for symbol in symbols:
        row = {
            "pe": None,
            "forward_pe": None,
            "eps_ttm": None,
            "revenue_growth_yoy": None,
            "gross_margin": None,
            "market_cap": None,
            "next_earnings_date": None,
        }
        try:
            data = _yahoo_quote_summary(symbol)
            if data:
                # summaryDetail: trailingPE, marketCap
                sd = data.get("summaryDetail") or {}
                row["pe"] = _raw_val(sd, "trailingPE")
                row["market_cap"] = _raw_val(sd, "marketCap")

                # defaultKeyStatistics: forwardPE, trailingEps
                dks = data.get("defaultKeyStatistics") or {}
                row["forward_pe"] = _raw_val(dks, "forwardPE")
                row["eps_ttm"] = _raw_val(dks, "trailingEps")

                # financialData: revenueGrowth, grossMargins
                fd = data.get("financialData") or {}
                row["revenue_growth_yoy"] = _raw_val(fd, "revenueGrowth")
                row["gross_margin"] = _raw_val(fd, "grossMargins")

                # calendarEvents: earnings.earningsDate[0]
                ce = data.get("calendarEvents") or {}
                earnings_list = ((ce.get("earnings") or {}).get("earningsDate") or [])
                if earnings_list:
                    raw_ts = earnings_list[0].get("raw")
                    if raw_ts:
                        try:
                            row["next_earnings_date"] = dt.datetime.fromtimestamp(
                                raw_ts, dt.timezone.utc
                            ).strftime("%Y-%m-%d")
                        except Exception:
                            pass
            else:
                print(f"  [fundamentals] {symbol}: empty quoteSummary, storing NULLs")

        except RuntimeError as exc:
            # Likely 401 crumb issue
            print(f"  [fundamentals] {symbol}: {exc}", file=sys.stderr)
            if _yfinance_available():
                print(f"  [fundamentals] {symbol}: falling back to yfinance…")
                row.update(_yf_fallback(symbol))
            else:
                print(
                    f"  [fundamentals] {symbol}: yfinance not installed — storing NULLs. "
                    "Install yfinance for fallback support.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"  [fundamentals] {symbol}: {exc} — storing NULLs", file=sys.stderr)

        try:
            con.execute(
                """insert into fundamentals
                   (symbol, pe, forward_pe, eps_ttm, revenue_growth_yoy,
                    gross_margin, market_cap, next_earnings_date, updated_at)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict(symbol) do update set
                       pe=excluded.pe,
                       forward_pe=excluded.forward_pe,
                       eps_ttm=excluded.eps_ttm,
                       revenue_growth_yoy=excluded.revenue_growth_yoy,
                       gross_margin=excluded.gross_margin,
                       market_cap=excluded.market_cap,
                       next_earnings_date=excluded.next_earnings_date,
                       updated_at=excluded.updated_at""",
                (
                    symbol,
                    row["pe"], row["forward_pe"], row["eps_ttm"],
                    row["revenue_growth_yoy"], row["gross_margin"],
                    row["market_cap"], row["next_earnings_date"],
                    now_str,
                ),
            )
            con.commit()
            total_upserted += 1
            non_null = sum(1 for v in row.values() if v is not None)
            print(f"  [fundamentals] {symbol}: upserted ({non_null}/7 fields non-null)")
        except sqlite3.Error as exc:
            print(f"  [fundamentals] {symbol}: DB error {exc}", file=sys.stderr)

        time.sleep(pause)

    print(f"[fundamentals] Done — {total_upserted} symbols upserted.")
    con.close()


# ---------------------------------------------------------------------------
# Benchmarks ingestion (R3-2)
# ---------------------------------------------------------------------------

def fetch_benchmarks(days_back: int = 730) -> None:
    """
    Fetch ~2 years of daily OHLCV for SPY, SOXX, QQQ into the prices table.

    Idempotent incremental: skips existing dates.  Uses yfinance (v1.5.1).
    Benchmark symbols are defined in BENCHMARK_SYMBOLS and are excluded from
    all universe queries (symbol_list, snapshot, hit-rate, /api/symbols).
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[benchmarks] yfinance is not installed — cannot fetch benchmarks.", file=sys.stderr)
        return

    con = connect()
    total_inserted = 0
    today = dt.datetime.now(dt.timezone.utc)

    try:
        for symbol in sorted(BENCHMARK_SYMBOLS):
            try:
                # Determine incremental start date
                row = con.execute(
                    "select max(date) from prices where symbol=?", (symbol,)
                ).fetchone()
                latest_date_str = row[0] if row else None

                if latest_date_str:
                    latest_dt = dt.datetime.strptime(latest_date_str, "%Y-%m-%d").replace(
                        tzinfo=dt.timezone.utc
                    )
                    days_diff = (today - latest_dt).days
                    if days_diff <= 1:
                        print(f"benchmark {symbol} - already up to date (latest: {latest_date_str})")
                        continue
                    start_date = (latest_dt + dt.timedelta(days=1)).date()
                    print(f"benchmark {symbol} - incremental from {start_date}")
                else:
                    start_date = (today - dt.timedelta(days=days_back)).date()
                    print(f"benchmark {symbol} - full fetch from {start_date}")

                end_date = (today + dt.timedelta(days=2)).date()
                tk = yf.Ticker(symbol)
                hist = tk.history(
                    start=start_date.isoformat(),
                    end=end_date.isoformat(),
                    interval="1d",
                    auto_adjust=True,
                )

                if hist is None or hist.empty:
                    print(f"  {symbol}: no data returned")
                    continue

                inserted = 0
                for idx, bar_row in hist.iterrows():
                    try:
                        date_str = idx.date().isoformat()
                    except AttributeError:
                        date_str = str(idx)[:10]

                    close_v = bar_row.get("Close")
                    if close_v is None:
                        continue
                    try:
                        close_f = float(close_v)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(close_f) or close_f <= 0:
                        continue

                    def _safe(v):
                        if v is None:
                            return None
                        try:
                            f = float(v)
                            return f if math.isfinite(f) and f > 0 else None
                        except (TypeError, ValueError):
                            return None

                    open_f  = _safe(bar_row.get("Open"))
                    high_f  = _safe(bar_row.get("High"))
                    low_f   = _safe(bar_row.get("Low"))
                    vol_v   = bar_row.get("Volume")
                    vol_i   = int(vol_v) if vol_v is not None else None

                    con.execute(
                        """insert or replace into prices(symbol, date, open, high, low, close, volume)
                           values (?, ?, ?, ?, ?, ?, ?)""",
                        (symbol, date_str, open_f, high_f, low_f, close_f, vol_i),
                    )
                    inserted += 1

                con.commit()
                print(f"  {symbol}: {inserted} bars inserted/updated")
                total_inserted += inserted
                time.sleep(0.5)

            except Exception as exc:
                print(f"  {symbol}: failed — {exc}", file=sys.stderr)

        print(f"[benchmarks] Done — {total_inserted} total bars inserted/updated.")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Analyst estimates ingestion (R3-3)
# ---------------------------------------------------------------------------

def fetch_estimates(min_mentions: int = 2, pause: float = 1.0) -> None:
    """
    Fetch analyst price targets and EPS estimates via yfinance for all tracked
    symbols (excluding BENCHMARK_SYMBOLS).

    Data sources used (yfinance 1.5.1 confirmed):
      - tk.info: targetMeanPrice, targetMedianPrice, targetHighPrice,
                 targetLowPrice, numberOfAnalystOpinions, recommendationKey,
                 recommendationMean
      - tk.earnings_estimate: avg EPS for 0q / +1q / 0y periods
      - tk.eps_revisions: upLast30days / downLast30days for 0q

    All missing fields → NULL.  Per-symbol try/except for graceful degradation.
    Idempotent upsert per symbol.
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[estimates] yfinance is not installed — cannot fetch estimates.", file=sys.stderr)
        return

    con = connect()
    now_str = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    symbols = symbol_list(con, min_mentions)

    if not symbols:
        print("[estimates] No symbols found — run fetch-x first.")
        con.close()
        return

    print(f"[estimates] Processing {len(symbols)} symbols…")
    total_upserted = 0

    for symbol in symbols:
        row: dict = {
            "target_mean":            None,
            "target_median":          None,
            "target_high":            None,
            "target_low":             None,
            "n_analysts":             None,
            "recommendation_key":     None,
            "recommendation_mean":    None,
            "eps_estimate_current_q": None,
            "eps_estimate_next_q":    None,
            "eps_estimate_current_y": None,
            "up_revisions_30d":       None,
            "down_revisions_30d":     None,
        }

        try:
            tk = yf.Ticker(symbol)
            info = tk.info or {}

            # Price targets and recommendation from .info
            def _fv(key):
                """Return float value from info or None."""
                v = info.get(key)
                if v is None:
                    return None
                try:
                    f = float(v)
                    return f if math.isfinite(f) else None
                except (TypeError, ValueError):
                    return None

            row["target_mean"]         = _fv("targetMeanPrice")
            row["target_median"]       = _fv("targetMedianPrice")
            row["target_high"]         = _fv("targetHighPrice")
            row["target_low"]          = _fv("targetLowPrice")
            row["recommendation_mean"] = _fv("recommendationMean")

            n_op = info.get("numberOfAnalystOpinions")
            if n_op is not None:
                try:
                    row["n_analysts"] = int(n_op)
                except (TypeError, ValueError):
                    pass

            rkey = info.get("recommendationKey")
            if rkey:
                row["recommendation_key"] = str(rkey).strip()

            # EPS estimates from earnings_estimate DataFrame
            try:
                ee = tk.earnings_estimate
                if ee is not None and not ee.empty and "avg" in ee.columns:
                    def _ee_val(period_key):
                        if period_key in ee.index:
                            v = ee.loc[period_key, "avg"]
                            try:
                                f = float(v)
                                return f if math.isfinite(f) else None
                            except (TypeError, ValueError):
                                return None
                        return None
                    row["eps_estimate_current_q"] = _ee_val("0q")
                    row["eps_estimate_next_q"]    = _ee_val("+1q")
                    row["eps_estimate_current_y"] = _ee_val("0y")
            except Exception as exc_ee:
                print(f"  [estimates] {symbol}: earnings_estimate error: {exc_ee}", file=sys.stderr)

            # Revision direction from eps_revisions DataFrame
            try:
                er = tk.eps_revisions
                if er is not None and not er.empty:
                    period_key = "0q" if "0q" in er.index else (er.index[0] if len(er.index) > 0 else None)
                    if period_key is not None:
                        def _er_int(col):
                            if col in er.columns:
                                v = er.loc[period_key, col]
                                try:
                                    return int(float(v))
                                except (TypeError, ValueError):
                                    return None
                            return None
                        row["up_revisions_30d"]   = _er_int("upLast30days")
                        row["down_revisions_30d"] = _er_int("downLast30days")
            except Exception as exc_er:
                print(f"  [estimates] {symbol}: eps_revisions error: {exc_er}", file=sys.stderr)

            non_null = sum(1 for v in row.values() if v is not None)
            print(f"  [estimates] {symbol}: fetched ({non_null}/12 fields non-null)")

        except Exception as exc:
            print(f"  [estimates] {symbol}: {exc} — storing NULLs", file=sys.stderr)

        # Idempotent upsert
        try:
            con.execute(
                """insert into analyst_estimates
                   (symbol, target_mean, target_median, target_high, target_low,
                    n_analysts, recommendation_key, recommendation_mean,
                    eps_estimate_current_q, eps_estimate_next_q, eps_estimate_current_y,
                    up_revisions_30d, down_revisions_30d, updated_at)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict(symbol) do update set
                       target_mean=excluded.target_mean,
                       target_median=excluded.target_median,
                       target_high=excluded.target_high,
                       target_low=excluded.target_low,
                       n_analysts=excluded.n_analysts,
                       recommendation_key=excluded.recommendation_key,
                       recommendation_mean=excluded.recommendation_mean,
                       eps_estimate_current_q=excluded.eps_estimate_current_q,
                       eps_estimate_next_q=excluded.eps_estimate_next_q,
                       eps_estimate_current_y=excluded.eps_estimate_current_y,
                       up_revisions_30d=excluded.up_revisions_30d,
                       down_revisions_30d=excluded.down_revisions_30d,
                       updated_at=excluded.updated_at""",
                (
                    symbol,
                    row["target_mean"], row["target_median"],
                    row["target_high"], row["target_low"],
                    row["n_analysts"], row["recommendation_key"],
                    row["recommendation_mean"],
                    row["eps_estimate_current_q"], row["eps_estimate_next_q"],
                    row["eps_estimate_current_y"],
                    row["up_revisions_30d"], row["down_revisions_30d"],
                    now_str,
                ),
            )
            con.commit()
            total_upserted += 1
        except Exception as exc_db:
            print(f"  [estimates] {symbol}: DB error {exc_db}", file=sys.stderr)

        time.sleep(pause)

    print(f"[estimates] Done — {total_upserted} symbols upserted.")
    con.close()


def main():
    ap = argparse.ArgumentParser(description="Ingest Serenity X posts, symbols and Yahoo prices into SQLite.")
    ap.add_argument("command", choices=[
        "fetch-x", "prices", "all", "stats", "stocktwits", "news", "fundamentals",
        "benchmarks", "estimates",
    ])
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--days", type=int, default=420)
    ap.add_argument("--days-back", type=int, default=730, help="History days for benchmarks fetch")
    ap.add_argument("--min-mentions", type=int, default=2)
    ap.add_argument("--symbol", help="Single symbol for stocktwits subcommand")
    ap.add_argument("--pause", type=float, default=1.0, help="Seconds between requests")
    args = ap.parse_args()
    if args.command in {"fetch-x", "all"}:
        fetch_x(args.max_pages)
    if args.command in {"prices", "all"}:
        fetch_prices(args.days, args.min_mentions)
    if args.command in {"stocktwits", "all"}:
        if args.symbol:
            con = connect()
            n = fetch_stocktwits(args.symbol.upper(), con=con)
            con.commit()
            if n is None:
                sys.exit(1)
        else:
            fetch_stocktwits_all(args.min_mentions, args.pause)
    if args.command == "news":
        fetch_news(min_mentions=args.min_mentions, pause=args.pause)
    if args.command == "fundamentals":
        fetch_fundamentals(min_mentions=args.min_mentions, pause=args.pause)
    if args.command == "benchmarks":
        fetch_benchmarks(days_back=args.days_back)
    if args.command == "estimates":
        fetch_estimates(min_mentions=args.min_mentions, pause=args.pause)
    if args.command == "stats":
        con = connect()
        try:
            print("tweets", con.execute("select count(*) from tweets").fetchone()[0])
            print("mentions", con.execute("select count(*) from mentions").fetchone()[0])
            print("prices", con.execute("select count(*) from prices").fetchone()[0])
            print("news_sentiment", con.execute("select count(*) from news_sentiment").fetchone()[0])
            print("news", con.execute("select count(*) from news").fetchone()[0])
            print("fundamentals", con.execute("select count(*) from fundamentals").fetchone()[0])
            ae_cnt = 0
            try:
                ae_cnt = con.execute("select count(*) from analyst_estimates").fetchone()[0]
            except Exception:
                pass
            print("analyst_estimates", ae_cnt)
            for row in con.execute("select symbol, count(*) c, min(mentioned_at), max(mentioned_at) from mentions group by symbol order by c desc, symbol"):
                print(dict(row))
        finally:
            con.close()


if __name__ == "__main__":
    main()
