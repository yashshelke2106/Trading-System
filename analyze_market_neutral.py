"""
analyze_market_neutral.py — is the journal's honest +0.63%/trade ALPHA or just BETA?

The backfilled journal now has clean spot P&L (spot_pnl_pct) + the holding window
(spot_entry_ts/spot_exit_ts). This subtracts NIFTY's return over each trade's exact
holding window (direction-signed: long = +NIFTY exposure, short = -NIFTY) to get the
market-NEUTRAL excess. If the edge was real, excess expectancy survives; if it was
"longs in a 2026 bull," excess collapses to ~0 or negative.

Run:  .venv/Scripts/python.exe analyze_market_neutral.py
"""
from __future__ import annotations

import json
import sys

import pandas as pd

sys.path.insert(0, ".")
from core.bar_cache import cached_daily

RESOLVED = ("SL_HIT", "TARGET_HIT", "TIME_EXIT")


def metrics(v):
    if not v:
        return (0, 0.0, 0.0, 0.0)
    wins = [x for x in v if x > 0]; losses = [x for x in v if x < 0]
    pf = (sum(wins) / -sum(losses)) if losses else float("inf")
    return (len(v), 100 * len(wins) / len(v), sum(v) / len(v), pf)


def main() -> int:
    rows = [json.loads(l) for l in open("logs/signal_journal.jsonl", encoding="utf-8") if l.strip()]
    nifty = None
    for sym in ("NIFTY", "NIFTY50", "NIFTY 50"):
        nifty = cached_daily(sym, 3000)
        if nifty is not None and len(nifty) > 200:
            print(f"[MN] NIFTY proxy = {sym} ({len(nifty)} bars)"); break
    if nifty is None or len(nifty) < 200:
        print("[MN] FATAL: no NIFTY bars."); return 1
    nclose = nifty["close"].copy()
    nclose.index = pd.to_datetime(nclose.index).normalize()
    nclose = nclose.sort_index()

    def asof(dt):
        s = nclose[nclose.index <= dt]
        return float(s.iloc[-1]) if len(s) else None

    raw, exc, mkt = [], [], []
    by = {"long": {"raw": [], "exc": []}, "short": {"raw": [], "exc": []}}
    used = 0
    for r in rows:
        sp = r.get("spot_pnl_pct"); so = r.get("spot_outcome")
        e1, e2 = r.get("spot_entry_ts"), r.get("spot_exit_ts")
        if not (isinstance(sp, (int, float)) and so in RESOLVED and e1 and e2):
            continue
        n1, n2 = asof(pd.Timestamp(e1)), asof(pd.Timestamp(e2))
        if not n1 or not n2 or n1 <= 0:
            continue
        nret = (n2 / n1 - 1.0) * 100.0
        dirn = 1.0 if str(r.get("direction", "long")).lower() == "long" else -1.0
        market = dirn * nret            # the position's market exposure
        excess = sp - market
        raw.append(sp); exc.append(excess); mkt.append(market)
        d = "long" if dirn > 0 else "short"
        by[d]["raw"].append(sp); by[d]["exc"].append(excess)
        used += 1

    n, wr, exp, pf = metrics(raw)
    ne, wre, expe, pfe = metrics(exc)
    avg_mkt = sum(mkt) / len(mkt) if mkt else 0
    print(f"\n[MN] {used} resolved trades with a NIFTY-matched window")
    print(f"  avg market exposure per trade: {avg_mkt:+.3f}%  (this is the beta the raw number rode)")
    print("\n  metric            RAW spot     MARKET-NEUTRAL excess")
    print(f"  win rate          {wr:5.1f}%          {wre:5.1f}%")
    print(f"  expectancy        {exp:+.3f}%/tr       {expe:+.3f}%/tr")
    print(f"  profit factor     {pf:5.2f}           {pfe:5.2f}")
    print("\n  by direction (expectancy raw -> neutral):")
    for d in ("long", "short"):
        _, _, er, _ = metrics(by[d]["raw"]); _, _, ee, _ = metrics(by[d]["exc"])
        print(f"    {d:5s} n={len(by[d]['raw']):4d}   {er:+.3f}%  ->  {ee:+.3f}%")

    print("\n  VERDICT")
    if expe > 0.15 and pfe > 1.3:
        print(f"  ALPHA SURVIVES: excess {expe:+.3f}%/tr (PF {pfe:.2f}) after removing market.")
        print("  Worth the matched-null + cost + survivorship gauntlet before believing it.")
    elif expe <= 0.05:
        print(f"  IT WAS BETA: excess collapses to {expe:+.3f}%/tr. The raw +{exp:.2f}% was")
        print("  mostly being long in a rising market, not signal skill. No directional edge.")
    else:
        print(f"  MARGINAL: excess {expe:+.3f}%/tr — thin, likely dies to costs/survivorship.")
    print("  (Caveat: beta=1 demean; legacy-engine sample; survivorship still uncorrected.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
