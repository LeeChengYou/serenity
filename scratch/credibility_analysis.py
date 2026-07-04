#!/usr/bin/env python3
"""
Credibility analysis of @aleabitoreddit's public X mentions.

For every (symbol, mention-day) event: compute the T+20-trading-day forward
return from real stored closes, and the cross-sectional EXCESS return vs the
same-date universe median (controls for market regime — a mention is only
"skillful" if the stock beat what everything else did over the same window).

All data real; no fabrication. Events without 20 forward bars are skipped.
"""
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "serenity.sqlite"
HORIZON = 20  # trading days


def load():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select symbol, date, close from prices "
        "where close is not null and close > 0 order by symbol, date"
    ).fetchall()
    prices = defaultdict(list)
    for s, d, c in rows:
        prices[s].append((d, float(c)))
    mentions = con.execute(
        "select distinct m.symbol, substr(m.mentioned_at,1,10) d, t.source "
        "from mentions m join tweets t on t.tweet_id=m.tweet_id "
        "order by m.symbol, d"
    ).fetchall()
    con.close()
    return prices, mentions


def fwd_return(plist, day, horizon=HORIZON):
    idx = None
    for i, (d, _) in enumerate(plist):
        if d >= day:
            idx = i
            break
    if idx is None or idx + horizon >= len(plist):
        return None
    return plist[idx + horizon][1] / plist[idx][1] - 1


def main():
    prices, mentions = load()
    # Universe median T+20 per date (cache)
    uni_cache = {}

    def universe_median(day):
        if day in uni_cache:
            return uni_cache[day]
        rets = [r for r in (fwd_return(pl, day) for pl in prices.values()) if r is not None]
        med = statistics.median(rets) if len(rets) >= 10 else None
        uni_cache[day] = med
        return med

    events = []          # (symbol, day, source, ret, excess)
    first_mention = {}   # symbol -> earliest day
    for sym, day, source in mentions:
        if sym not in prices:
            continue
        first_mention.setdefault(sym, day)
        r = fwd_return(prices[sym], day)
        if r is None:
            continue
        u = universe_median(day)
        if u is None:
            continue
        events.append((sym, day, source, r, r - u))

    print(f"=== ALL public mention events with T+{HORIZON} coverage: n={len(events)} ===")
    rets = [e[3] for e in events]
    exc = [e[4] for e in events]
    print(f"absolute : median {statistics.median(rets)*100:+.1f}%  "
          f"win rate {sum(1 for r in rets if r > 0)/len(rets)*100:.0f}%")
    print(f"vs universe (excess): median {statistics.median(exc)*100:+.1f}%  "
          f"beat-universe rate {sum(1 for e in exc if e > 0)/len(exc)*100:.0f}%")

    print("\n=== by source ===")
    for src in ("posts", "replies"):
        se = [e for e in events if e[2] == src]
        if len(se) < 5:
            print(f"{src}: insufficient (n={len(se)})")
            continue
        sr = [e[3] for e in se]
        sx = [e[4] for e in se]
        print(f"{src:8s}: n={len(se):3d}  median {statistics.median(sr)*100:+.1f}%  "
              f"excess {statistics.median(sx)*100:+.1f}%  "
              f"beat-rate {sum(1 for e in sx if e > 0)/len(sx)*100:.0f}%")

    print("\n=== by month cohort (consistency check) ===")
    coh = defaultdict(list)
    for _, day, _, r, x in events:
        coh[day[:7]].append(x)
    for m in sorted(coh):
        v = coh[m]
        if len(v) < 5:
            print(f"{m}: n={len(v)} insufficient")
            continue
        print(f"{m}: n={len(v):3d}  median excess {statistics.median(v)*100:+.1f}%  "
              f"beat-rate {sum(1 for e in v if e > 0)/len(v)*100:.0f}%")

    print("\n=== FIRST-mention events only (his 'new idea' calls) ===")
    fe = [e for e in events if first_mention.get(e[0]) == e[1]]
    if len(fe) >= 10:
        fr = [e[3] for e in fe]
        fx = [e[4] for e in fe]
        print(f"n={len(fe)}  median {statistics.median(fr)*100:+.1f}%  "
              f"excess {statistics.median(fx)*100:+.1f}%  "
              f"beat-rate {sum(1 for e in fx if e > 0)/len(fx)*100:.0f}%")
        ranked = sorted(fe, key=lambda e: e[4], reverse=True)
        print("best 5 first calls :", [(e[0], e[1], f"{e[4]*100:+.0f}%") for e in ranked[:5]])
        print("worst 5 first calls:", [(e[0], e[1], f"{e[4]*100:+.0f}%") for e in ranked[-5:]])
    else:
        print(f"insufficient (n={len(fe)})")


if __name__ == "__main__":
    main()
