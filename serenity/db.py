"""
serenity/db.py
db(), _init_schema, _schema_initialized, _table_exists, one()
（原 server.py 305-466 行 + 1499-1505 行）
"""
import sqlite3
from .config import DB_PATH

_schema_initialized = False


def db():
    global _schema_initialized
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    if not _schema_initialized:
        _init_schema(con)
        _schema_initialized = True
    return con


def _init_schema(con):
    con.executescript("""
        create table if not exists user_memories (
            id integer primary key autoincrement,
            category text not null,
            symbol text,
            content text not null,
            weight real default 1.0,
            updated_at text not null,
            unique(category, symbol, content)
        );
        create table if not exists scorecards (
            symbol text primary key,
            company text,
            market text,
            final_score real,
            verdict text,
            raw_factor_points real,
            penalty_points real,
            factors_json text,
            penalties_json text,
            evidence_json text,
            kill_switches_json text,
            updated_at text
        );
        create index if not exists idx_user_memories_weight on user_memories(weight desc);
        create index if not exists idx_scorecards_symbol on scorecards(symbol);
    """)
    # Idempotent: scorecard_history table (SPEC F-03)
    con.execute("""
        create table if not exists scorecard_history (
            id          integer primary key autoincrement,
            symbol      text not null,
            final_score real not null,
            verdict     text,
            factors_json text,
            penalties_json text,
            model_used  text,
            created_at  text default (datetime('now'))
        )
    """)
    # R-5: signal_history — daily snapshots for hit-rate tracking (idempotent)
    con.execute("""
        create table if not exists signal_history (
            symbol      text not null,
            date        text not null,
            signal      text,
            score       real,
            score_source text,
            close       real,
            rsi         real,
            atr14       real,
            primary key (symbol, date)
        )
    """)
    # R-3: dossier cache — avoids re-billing Gemini on every view (idempotent)
    con.execute("""
        create table if not exists dossiers (
            symbol      text primary key,
            dossier_json text not null,
            created_at  text not null
        )
    """)
    # R3-5: signal_changes — tracks daily signal transitions (idempotent)
    con.execute("""
        create table if not exists signal_changes (
            symbol      text not null,
            date        text not null,
            prev_signal text,
            new_signal  text,
            primary key (symbol, date)
        )
    """)
    # R3-1: hitrate_cache — caches point-in-time reconstruction results (idempotent)
    con.execute("""
        create table if not exists hitrate_cache (
            cache_key      text primary key,
            max_price_date text not null,
            cache_json     text not null,
            computed_at    text not null
        )
    """)
    # R3-3: analyst_estimates — price targets and EPS estimates (idempotent)
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
    try:
        con.execute(
            "create index if not exists idx_scorecard_history_symbol "
            "on scorecard_history (symbol, created_at)"
        )
    except Exception:
        pass
    # R4-2: translation cache (idempotent)
    con.execute("""
        create table if not exists translations (
            src_hash       text primary key,
            src_text       text not null,
            translated_text text not null,
            model          text,
            created_at     text not null
        )
    """)
    # R5-2: expert_views — credible manager holdings from EDGAR 13F etc (idempotent)
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
    con.execute("""
        create index if not exists idx_expert_views_published
            on expert_views(published_at desc)
    """)
    for idx_name, tbl_name, col in [
        ("idx_mentions_symbol", "mentions", "symbol"),
        ("idx_prices_symbol_date", "prices", "symbol, date"),
        ("idx_tweets_created", "tweets", "created_at")
    ]:
        try:
            con.execute(f"create index if not exists {idx_name} on {tbl_name}({col})")
        except Exception:
            pass
    con.commit()


def _table_exists(con, table_name: str) -> bool:
    """Return True if the named table exists in the database."""
    row = con.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def one(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else {}
