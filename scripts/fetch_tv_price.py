#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

from tradingview_scraper.symbols.stream import Streamer

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"


def fetch_ohlc(exchange: str, tv_symbol: str, timeframe: str, candles: int):
    logging.getLogger("tradingview_scraper").setLevel(logging.CRITICAL)
    logging.getLogger("websocket").setLevel(logging.CRITICAL)
    streamer = Streamer(export_result=False)
    result = streamer.stream(
        exchange=exchange,
        symbol=tv_symbol,
        timeframe=timeframe,
        numb_price_candles=candles,
    )
    if isinstance(result, dict):
        return result.get("ohlc") or []

    # export_result=False returns a generator; keep this path for package API changes.
    for packet in result:
        data = streamer._extract_ohlc_from_stream(packet)
        if data:
            return data
    return []


def _guard_float(val):
    """Return float if val is finite and within a sane price range, else None."""
    import math
    if val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) and 0 <= f <= 100000 else None
    except (TypeError, ValueError):
        return None


def save_prices(rows, db_symbol: str):
    """
    Persist TradingView OHLCV rows into the prices table.

    The TradingView scraper already provides full OHLCV data; this function
    stores open/high/low in addition to close/volume so that technical
    indicators can be computed.  Applies the same None/NaN/negative guards
    used in ingest.py::fetch_prices().
    """
    con = sqlite3.connect(DB_PATH)
    con.execute("pragma journal_mode=wal")
    try:
        con.execute("""
            create table if not exists prices (
                symbol text not null,
                date text not null,
                open real,
                high real,
                low real,
                close real not null,
                volume integer,
                unique(symbol, date)
            );
        """)
        # Idempotent migration: add OHLC columns if this DB predates OHLC support.
        existing = {r[1] for r in con.execute("PRAGMA table_info(prices)")}
        for col in ("open", "high", "low"):
            if col not in existing:
                con.execute(f"ALTER TABLE prices ADD COLUMN {col} REAL")
        con.commit()

        inserted = 0
        for row in rows:
            if "timestamp" not in row:
                continue
            close_f = _guard_float(row.get("close"))
            if close_f is None or close_f <= 0:
                continue  # skip bars without a valid close — never fabricate
            date = dt.datetime.fromtimestamp(row["timestamp"], dt.timezone.utc).date().isoformat()
            con.execute(
                """insert or replace into prices(symbol, date, open, high, low, close, volume)
                   values (?, ?, ?, ?, ?, ?, ?)""",
                (
                    db_symbol.upper(),
                    date,
                    _guard_float(row.get("open")),
                    _guard_float(row.get("high")),
                    _guard_float(row.get("low")),
                    close_f,
                    int(row.get("volume") or 0) if row.get("volume") is not None else None,
                ),
            )
            inserted += 1
        con.commit()
        return inserted
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description="Fetch TradingView OHLC candles into the local SQLite price table.")
    parser.add_argument("--exchange", required=True, help="TradingView exchange, e.g. OTC")
    parser.add_argument("--tv-symbol", required=True, help="TradingView symbol, e.g. SIVEF")
    parser.add_argument("--db-symbol", required=True, help="Local dashboard symbol, e.g. SIVE")
    parser.add_argument("--timeframe", default="1d", help="1d, 1w, 1m, etc.; default: 1d")
    parser.add_argument("--candles", type=int, default=200)
    parser.add_argument("--also-save-tv-symbol", action="store_true")
    args = parser.parse_args()

    rows = fetch_ohlc(args.exchange.upper(), args.tv_symbol.upper(), args.timeframe, args.candles)
    if not rows:
        raise SystemExit("no TradingView OHLC rows returned")

    inserted = save_prices(rows, args.db_symbol)
    if args.also_save_tv_symbol and args.tv_symbol.upper() != args.db_symbol.upper():
        save_prices(rows, args.tv_symbol)

    first = dt.datetime.fromtimestamp(rows[0]["timestamp"], dt.timezone.utc).date().isoformat()
    last = dt.datetime.fromtimestamp(rows[-1]["timestamp"], dt.timezone.utc).date().isoformat()
    print(json.dumps({
        "tv_symbol": f"{args.exchange.upper()}:{args.tv_symbol.upper()}",
        "db_symbol": args.db_symbol.upper(),
        "rows": inserted,
        "first": first,
        "last": last,
        "last_close": rows[-1]["close"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
