"""
research_trend_hitrate.py — H-013: can the equity risk premium buy 7-in-10 profitably?

WHAT THE PREVIOUS STUDY LEFT OPEN
---------------------------------
research_hitrate_frontier.py (H-012 companion, commit dd96bd8) sampled entries
UNCONDITIONALLY — every Nth day regardless of setup. That correctly measures the
universe's random-walk baseline, and it found edge ~0 and every 70%+ cell
unprofitable. But it does not answer whether a CONDITIONAL entry can lift the
hit rate above the null.

One drift in this market is already validated and is not alpha: the equity risk
premium. Indian large-caps drift up ~12%/yr, i.e. ~0.05%/day, ~0.5% over a
10-day hold. Drift is precisely what shifts P(touch +T before -S) above the
driftless null S/(S+T). So the honest open question is:

    does trend-filtered, long-only entry lift the hit rate above the null by
    MORE than the cost drag, at a configuration that also reaches 70%?

This is a beta harvest, not an edge discovery. It is registered as H-013 and
must clear the same bars as anything else.

METHOD — identical to the unconditional study except the entry condition
-----------------------------------------------------------------------
  same grid, same universe, same survivorship-complete archive
  same PESSIMISTIC intrabar rule (ambiguous bar -> STOP wins)
  same null (S / (S + T))
  ENTRY now requires: close > 200-DMA (stock uptrend)
                      AND index proxy > its 200-DMA (market uptrend)
  Both conditions use only data available at the signal bar (no lookahead);
  entry is still the NEXT day's open.

The index filter matters: without it, "stock above its 200-DMA" still fires
through 2020-style crashes where every trend breaks at once.

RUN
---
    python research_trend_hitrate.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research_hitrate_frontier as BASE

MA_LEN = 200
SAMPLE_EVERY = 3      # denser sampling; the trend filter removes most days anyway


def build_market_filter(frames: Dict[str, pd.DataFrame]) -> pd.Series:
    """Equal-weight breadth proxy for 'is the market in an uptrend'.

    Built from the same archive rather than an external index so it is
    survivorship-consistent with the universe being traded. True when the
    proxy's own close is above its 200-DMA.
    """
    closes = pd.DataFrame({s: d["close"] for s, d in frames.items()})
    # Normalise each name to its own first value so no single high-priced
    # symbol dominates the average.
    norm = closes / closes.bfill().iloc[0]
    proxy = norm.mean(axis=1).dropna()
    ma = proxy.rolling(MA_LEN).mean()
    return (proxy > ma).reindex(proxy.index).fillna(False)


def simulate_trend(df: pd.DataFrame, tgt_pct: float, stop_pct: float,
                   mkt_ok: pd.Series) -> List[int]:
    """Long-only, trend-filtered. Pessimistic tie-break, next-open entry."""
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    idx = df.index
    ma = pd.Series(c, index=idx).rolling(MA_LEN).mean().values
    n = len(df)
    out: List[int] = []

    mkt = mkt_ok.reindex(idx).fillna(False).values

    for i in range(MA_LEN, n - BASE.MAX_HOLD - 1, SAMPLE_EVERY):
        entry_ref = o[i]
        if not np.isfinite(entry_ref) or entry_ref < BASE.MIN_PRICE:
            continue
        # Trend conditions evaluated on bar i-1 data only (close/MA of bar i
        # is known at bar i's close; entry is next open, so no lookahead).
        if not np.isfinite(ma[i]) or c[i] <= ma[i]:
            continue
        if not mkt[i]:
            continue

        entry = o[i + 1] if i + 1 < n else None
        if entry is None or not np.isfinite(entry) or entry <= 0:
            continue
        tgt = entry * (1 + tgt_pct / 100.0)
        stp = entry * (1 - stop_pct / 100.0)
        res = 0
        for k in range(i + 1, min(i + 1 + BASE.MAX_HOLD, n)):
            if l[k] <= stp:      # stop wins ties and wins outright
                res = 0
                break
            if h[k] >= tgt:
                res = 1
                break
        out.append(res)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.20)
    args = ap.parse_args()

    from core.universe import FO_UNIVERSE
    syms = [s.upper() for s in FO_UNIVERSE]

    print("=== H-013: trend-filtered long-only — can beta drift buy 7-in-10? ===")
    print(f"cost {args.cost:.2f}% round-trip | hold {BASE.MAX_HOLD}d | "
          f"pessimistic intrabar tie-break")
    print("entry: close > 200-DMA AND market proxy > its 200-DMA, next-open fill\n")

    frames = {}
    for s in syms:
        d = BASE.load_symbol(s)
        if d is not None and len(d) > 400:
            frames[s] = d
    print(f"loaded {len(frames)} symbols")

    mkt_ok = build_market_filter(frames)
    print(f"market filter ON for {mkt_ok.mean()*100:.1f}% of days\n")

    rows = []
    for tgt in BASE.TARGETS:
        for stp in BASE.STOPS:
            res: List[int] = []
            for d in frames.values():
                res.extend(simulate_trend(d, tgt, stp, mkt_ok))
            if not res:
                continue
            n = len(res)
            hit = sum(res) / n
            rr = tgt / stp
            p_null = stp / (stp + tgt)
            cost_r = args.cost / stp
            exp_r = hit * rr - (1 - hit) * 1.0 - cost_r
            rows.append({
                "target_pct": tgt, "stop_pct": stp, "rr": round(rr, 3), "n": n,
                "hit_rate": round(hit, 4), "p_null": round(p_null, 4),
                "edge": round(hit - p_null, 4), "cost_R": round(cost_r, 4),
                "exp_R": round(exp_r, 4),
                "hits_7of10": hit >= 0.70, "profitable": exp_r > 0,
            })

    df = pd.DataFrame(rows)
    print(f"{'tgt%':>5}{'stop%':>7}{'R:R':>6}{'n':>8}{'hit':>8}{'null':>8}"
          f"{'edge':>8}{'expR':>8}  flags")
    print("-" * 72)
    for _, r in df.iterrows():
        flags = []
        if r["hits_7of10"]:
            flags.append("7of10")
        if r["profitable"]:
            flags.append("PROFIT")
        print(f"{r['target_pct']:>5.1f}{r['stop_pct']:>7.1f}{r['rr']:>6.2f}"
              f"{int(r['n']):>8d}{r['hit_rate']*100:>7.1f}%{r['p_null']*100:>7.1f}%"
              f"{r['edge']*100:>+7.1f}%{r['exp_R']:>+8.3f}  {' '.join(flags)}")

    both = df[(df["hits_7of10"]) & (df["profitable"])]
    print("\n" + "=" * 72)
    print(f"cells reaching 7-in-10    : {int(df['hits_7of10'].sum())}")
    print(f"cells profitable          : {int(df['profitable'].sum())}")
    print(f"cells achieving BOTH      : {len(both)}")
    print(f"mean edge vs null         : {df['edge'].mean()*100:+.2f}%")
    if len(both):
        print("\n*** CONFIGURATIONS ACHIEVING 7-IN-10 *AND* PROFITABLE ***")
        for _, r in both.sort_values("exp_R", ascending=False).iterrows():
            print(f"  target {r['target_pct']}% / stop {r['stop_pct']}%  "
                  f"-> hit {r['hit_rate']*100:.1f}%  edge {r['edge']*100:+.1f}%  "
                  f"{r['exp_R']:+.3f}R/trade  (n={int(r['n'])})")
    else:
        print("\nNo configuration achieves both.")

    os.makedirs("docs/research", exist_ok=True)
    df.to_json("docs/research/trend_hitrate_H013.json", orient="records", indent=2)
    print("\nwrote docs/research/trend_hitrate_H013.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
