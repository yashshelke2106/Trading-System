"""
research_hitrate_frontier.py — can this system hit 7-in-10 targets AND make money?

THE QUESTION
------------
A 70% hit rate is trivially achievable: set a tiny target and a wide stop. It is
also usually worthless. Win rate is only meaningful paired with reward:risk and
net of costs. So the real question is not "can we hit 70%" but:

    is there a (target, stop) pair whose REAL hit rate is 70%+ AND whose
    expectancy is positive after costs?

THE NULL THAT MAKES THIS RIGOROUS
---------------------------------
For a driftless random walk, the probability of touching +T before -S is

    P_null = S / (S + T)

and the break-even win rate for that same pair is

    WR_breakeven = 1 / (1 + T/S) = S / (S + T)

They are IDENTICAL. A coin-flip market therefore yields exactly zero expectancy
at EVERY reward:risk ratio — you cannot engineer profit by choosing levels. Any
positive expectancy must come from the real hit rate EXCEEDING P_null, i.e. from
genuine drift/edge. Costs then subtract from whatever surplus exists.

So this script measures, over a grid of (target, stop):
  - actual hit rate on real bars
  - P_null for that pair
  - edge = actual - P_null            <- the only thing that can pay
  - expectancy in R, net of costs
and reports whether ANY cell clears 70% hit rate with positive net expectancy.

INTRABAR HONESTY (the bias that would otherwise fake a win)
-----------------------------------------------------------
With daily bars, when a day's range contains BOTH the target and the stop, the
order of touch is unknowable. Assuming target-first would manufacture a large
fake edge — this is the classic mirage. This script assumes STOP FIRST in every
ambiguous bar. That is pessimistic by construction and it is the only honest
choice without intraday data.

UNIVERSE
--------
F&O large-caps from the survivorship-complete bhavcopy archive (the tradeable
set), entry at each day's OPEN, walk forward up to MAX_HOLD days.

RUN
---
    python research_hitrate_frontier.py
    python research_hitrate_frontier.py --cost 0.20
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

SYMBOL_DIR = os.path.join("logs", "bhavcopy_archive", "symbols")

MAX_HOLD = 10          # trading days to reach target or stop
MIN_PRICE = 50.0
SAMPLE_EVERY = 5       # take an entry every N days (decorrelates observations)

# Grid: target% and stop% (of entry price)
TARGETS = [0.5, 1.0, 1.5, 2.0, 3.0]
STOPS = [1.0, 2.0, 3.0, 5.0]


def load_symbol(sym: str) -> Optional[pd.DataFrame]:
    p = os.path.join(SYMBOL_DIR, f"{sym}.parquet")
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index)
        need = ("open", "high", "low", "close")
        if not all(c in df.columns for c in need):
            return None
        return df[list(need)].sort_index()
    except Exception:
        return None


def simulate(df: pd.DataFrame, tgt_pct: float, stop_pct: float) -> List[int]:
    """Walk forward from sampled entries. 1 = target first, 0 = stop/timeout.

    Ambiguous bars (both levels inside the same day's range) resolve to STOP —
    pessimistic by construction, because daily bars cannot order the touches.
    """
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    n = len(df)
    out: List[int] = []

    for i in range(0, n - MAX_HOLD - 1, SAMPLE_EVERY):
        entry = o[i]
        if not np.isfinite(entry) or entry < MIN_PRICE:
            continue
        tgt = entry * (1 + tgt_pct / 100.0)
        stp = entry * (1 - stop_pct / 100.0)
        res = 0
        for k in range(i, i + MAX_HOLD):
            hit_t = h[k] >= tgt
            hit_s = l[k] <= stp
            if hit_s:            # stop wins ties AND wins outright
                res = 0
                break
            if hit_t:
                res = 1
                break
        out.append(res)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.20,
                    help="round-trip cost in %% (default 0.20 = futures-grade)")
    args = ap.parse_args()

    from core.universe import FO_UNIVERSE
    syms = [s.upper() for s in FO_UNIVERSE]

    print("=== HIT-RATE FRONTIER: can 7-in-10 be hit AND pay? ===")
    print(f"universe: {len(syms)} F&O large-caps | hold {MAX_HOLD}d | "
          f"cost {args.cost:.2f}% round-trip")
    print("ambiguous intrabar touches resolve to STOP (pessimistic)\n")

    frames = {}
    for s in syms:
        d = load_symbol(s)
        if d is not None and len(d) > 300:
            frames[s] = d
    print(f"loaded {len(frames)} symbols with usable history\n")

    rows = []
    for tgt in TARGETS:
        for stp in STOPS:
            results: List[int] = []
            for d in frames.values():
                results.extend(simulate(d, tgt, stp))
            if not results:
                continue
            n = len(results)
            hit = sum(results) / n
            rr = tgt / stp
            p_null = stp / (stp + tgt)          # random-walk touch probability
            edge = hit - p_null
            # Expectancy in R (stop = 1R), cost charged in R units
            cost_r = (args.cost / stp)
            exp_r = hit * rr - (1 - hit) * 1.0 - cost_r
            rows.append({
                "target_pct": tgt, "stop_pct": stp, "rr": round(rr, 3),
                "n": n, "hit_rate": round(hit, 4),
                "p_null": round(p_null, 4), "edge": round(edge, 4),
                "exp_R": round(exp_r, 4),
                "hits_7of10": hit >= 0.70,
                "profitable": exp_r > 0,
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
    seven = df[df["hits_7of10"]]
    prof = df[df["profitable"]]

    print("\n" + "=" * 72)
    print(f"cells reaching 7-in-10 hit rate      : {len(seven)}")
    print(f"cells with positive net expectancy   : {len(prof)}")
    print(f"cells achieving BOTH                 : {len(both)}")
    if len(both):
        print("\nBOTH (the answer to the goal):")
        for _, r in both.iterrows():
            print(f"  target {r['target_pct']}% / stop {r['stop_pct']}% "
                  f"-> hit {r['hit_rate']*100:.1f}%, {r['exp_R']:+.3f}R/trade")
    else:
        print("\nNo configuration achieves 7-in-10 AND positive expectancy.")

    mean_edge = df["edge"].mean()
    print(f"\nmean edge over random-walk null across grid: {mean_edge*100:+.2f}%")
    print("(edge is the ONLY source of profit; level choice alone cannot create it)")

    os.makedirs("docs/research", exist_ok=True)
    df.to_json("docs/research/hitrate_frontier.json", orient="records", indent=2)
    print("\nwrote docs/research/hitrate_frontier.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
