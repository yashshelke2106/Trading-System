"""
research_edge_required.py — the exact edge required, and whether it exists.

THE CLOSED FORM
---------------
A (target T, stop S) bracket is profitable when

    hit > (1 + c/S) / (1 + T/S)

and the driftless random-walk null is p0 = S/(S+T). Subtracting:

    EDGE REQUIRED  =  c / (T + S)

Only the SUM of the levels matters. Cost is a fixed toll; spreading it over a
wider bracket shrinks the edge you must supply to clear it.

To also reach a 70% hit rate the null itself must sit near 70%, i.e.
S/(S+T) >= 0.70  =>  S >= 2.333*T. Pinning that ratio and substituting:

    EDGE REQUIRED  =  c / (3.333 * T)

So the requirement falls as the bracket widens — but a wider bracket takes
longer to resolve, and at long horizons the dominant drift is the EQUITY RISK
PREMIUM, which is the one edge this programme has actually validated.

This script measures, on real bars, whether that premium supplies the required
edge at wide brackets — i.e. whether 7-in-10 profitably is reachable through
beta rather than alpha.

Levels are pinned to S = 2.333*T so the null sits at 70% by construction; the
only question at each width is whether the ACTUAL hit rate clears the null by
more than c/(T+S).

RUN
---
    python research_edge_required.py
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
MIN_PRICE = 50.0
SAMPLE_EVERY = 10
P_TARGET_FIRST = 0.904          # measured on 5m bars
STOP_RATIO = 2.3333             # S = 2.3333 * T  =>  null = 70%

# (target %, max hold days). Wider brackets need longer to resolve.
WIDTHS = [(1.5, 20), (3.0, 40), (6.0, 90), (9.0, 150), (12.0, 250)]


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


def simulate(df: pd.DataFrame, tgt: float, stp: float, hold: int):
    """Returns (n, pess_hits, opt_hits, n_unresolved)."""
    o, h, l = df["open"].values, df["high"].values, df["low"].values
    n = len(df)
    tot = pess = opt = unres = 0
    for i in range(0, n - hold - 1, SAMPLE_EVERY):
        e = o[i + 1]
        if not np.isfinite(e) or e < MIN_PRICE:
            continue
        T = e * (1 + tgt / 100.0)
        S = e * (1 - stp / 100.0)
        tot += 1
        done = False
        for k in range(i + 1, min(i + 1 + hold, n)):
            ht, hs = h[k] >= T, l[k] <= S
            if ht and hs:            # ambiguous bar
                opt += 1
                done = True
                break
            if hs:
                done = True
                break
            if ht:
                pess += 1
                opt += 1
                done = True
                break
        if not done:
            unres += 1               # timed out: counted as a non-hit
    return tot, pess, opt, unres


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.20)
    ap.add_argument("--universe", default="all", choices=["all", "fo"],
                    help="'all' = survivorship-complete archive (honest); "
                         "'fo' = today's F&O list (survivorship-BIASED arm)")
    args = ap.parse_args()
    c = args.cost

    if args.universe == "fo":
        # TODAY's F&O list applied to history = survivorship bias. Kept only
        # as the biased comparison arm, never as the headline.
        from core.universe import FO_UNIVERSE
        syms = [s.upper() for s in FO_UNIVERSE]
    else:
        # Survivorship-complete: every symbol the archive ever recorded,
        # including names that later delisted. This is the honest arm.
        import glob as _g
        syms = sorted(os.path.basename(p_)[:-8]
                      for p_ in _g.glob(os.path.join(SYMBOL_DIR, "*.parquet")))

    print("=== THE EXACT EDGE REQUIRED, AND WHETHER IT EXISTS ===")
    print(f"edge_required = cost / (target + stop);  cost = {c:.2f}% round-trip")
    print(f"stop pinned at {STOP_RATIO:.3f}x target so the null sits at 70%")
    print(f"intrabar ties blended at P(target first) = {P_TARGET_FIRST}\n")

    frames = {}
    for s in syms:
        d = load_symbol(s)
        if d is not None and len(d) > 400:
            frames[s] = d
    print(f"loaded {len(frames)} symbols\n")

    print(f"{'target':>7}{'stop':>8}{'hold':>7}{'n':>8}{'hit':>8}{'null':>7}"
          f"{'edge':>8}{'req':>8}{'margin':>9}{'expR':>9}  verdict")
    print("-" * 90)

    rows = []
    for tgt, hold in WIDTHS:
        stp = tgt * STOP_RATIO
        tot = pe = op = un = 0
        for d in frames.values():
            a, b, cc, dd = simulate(d, tgt, stp, hold)
            tot += a; pe += b; op += cc; un += dd
        if tot == 0:
            continue
        hit = (pe + P_TARGET_FIRST * (op - pe)) / tot
        p0 = stp / (stp + tgt)
        edge = hit - p0
        req = c / (tgt + stp)
        margin = edge - req
        rr = tgt / stp
        exp_r = hit * rr - (1 - hit) * 1.0 - (c / stp)
        ok = (hit >= 0.70) and (exp_r > 0)
        rows.append({"target": tgt, "stop": round(stp, 2), "hold": hold, "n": tot,
                     "hit": round(hit, 4), "null": round(p0, 4),
                     "edge": round(edge, 4), "required": round(req, 4),
                     "margin": round(margin, 4), "exp_R": round(exp_r, 4),
                     "unresolved_pct": round(un / tot, 3), "achieves": ok})
        print(f"{tgt:>6.1f}%{stp:>7.1f}%{hold:>6d}d{tot:>8d}{hit*100:>7.1f}%"
              f"{p0*100:>6.1f}%{edge*100:>+7.2f}%{req*100:>7.2f}%"
              f"{margin*100:>+8.2f}%{exp_r:>+9.4f}  "
              f"{'*** ACHIEVES 7-IN-10 + PROFIT ***' if ok else ''}")

    print()
    winners = [r for r in rows if r["achieves"]]
    if winners:
        print("=" * 90)
        print("THE EDGE EXISTS AT THESE WIDTHS:")
        for w in winners:
            print(f"  target {w['target']}% / stop {w['stop']}% / hold {w['hold']}d")
            print(f"    hit {w['hit']*100:.1f}%  (null {w['null']*100:.1f}%)  "
                  f"edge {w['edge']*100:+.2f}%  required {w['required']*100:.2f}%  "
                  f"margin {w['margin']*100:+.2f}%")
            print(f"    expectancy {w['exp_R']:+.4f}R per trade, n={w['n']}, "
                  f"{w['unresolved_pct']*100:.0f}% timed out")
    else:
        print("No width supplies the required edge.")

    os.makedirs("docs/research", exist_ok=True)
    with open("docs/research/edge_required.json", "w", encoding="utf-8") as fh:
        json.dump({"cost_pct": c, "stop_ratio": STOP_RATIO, "rows": rows}, fh, indent=2)
    print("\nwrote docs/research/edge_required.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
