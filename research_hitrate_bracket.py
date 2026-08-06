"""
research_hitrate_bracket.py — can 7-of-10 trades hit target with a 10% stop?

THE ANSWER IS YES, AND THAT IS THE PROBLEM
------------------------------------------
For a bracket with stop distance S and target distance T, a driftless walk
touches the target first with probability

    P(target first) = S / (S + T)

so ANY hit rate you name is reachable by choosing T. For S = 10%:

    70% hit rate  ->  T = 4.286%
    80% hit rate  ->  T = 2.500%

But the expected value of that bracket is

    EV = P*T - (1-P)*S = [S/(S+T)]*T - [T/(S+T)]*S = 0     exactly, always.

The hit rate and the payoff move in lockstep: buying a higher win rate costs
exactly what it gains. Level selection cannot manufacture profit -- only a
genuine directional edge ABOVE the random-walk null can. After costs the same
bracket is strictly negative, and the true hit rate needed to break even is

    WR_breakeven = (S + cost) / (S + T)

which for S=10%, T=4.286%, cost=0.24% is 71.68% against a 70.00% null: you
need +1.68 points of REAL edge, not a redrawn target.

WHAT THIS SCRIPT DOES
---------------------
Measures the actual outcome on point-in-time, corporate-action-cleaned NSE
data: for each entry, walk forward bar by bar and record which side was
touched first, using intraday HIGH/LOW rather than closes, because a bracket
is touched intraday or not at all.

Ambiguity: when a single bar's range spans BOTH levels, the true order is
unknowable from daily bars. Both readings are reported -- optimistic (target
first) and pessimistic (stop first) -- so the conclusion cannot hide inside
that assumption.

MEASURED RESULT, AND THE CONTROL THAT EXPLAINS IT (2026-08-06)
--------------------------------------------------------------
At target 4.286% / stop 10%, long-only, n=103,757:

    hit rate 74.3%  (vs 70.0% null)   net +0.381%/trade   ~+9.4%/yr

That clears the 71.68% break-even, so the goal is met. But the entries are
UNCONDITIONAL -- every liquid name every 5th day, no signal whatsoever -- so
the excess cannot be skill. The short-side control confirms it:

    LONG   hit 74.3%   net +0.381%/trade   ann  +9.4%/yr
    SHORT  hit 65.4%   net -0.899%/trade   ann -21.5%/yr

The two average to 69.85%, i.e. the 70% null. Upward drift is the entire
deviation: long harvests it, short pays it. There is no directional edge here.

And the sting: buy-and-hold on the same universe returns ~20%/yr, so the
bracket delivers a 74% win rate for roughly HALF the return. The 10% stop
keeps ejecting you from winners and the 4.286% target caps the ones that run.
A high hit rate is bought with return, every time.
"""

from __future__ import annotations

import glob
import os
from typing import Dict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(ROOT, "logs", "bhavcopy_archive", "daily")

STOP_PCT = 10.0
TARGETS = (2.5, 3.333, 4.286, 5.385, 6.667)   # -> 80/75/70/65/60% null hit rates
MAX_HOLD = 60                 # bars; a 10% stop needs room to resolve
TOP_N_LIQUID = 300
MIN_PRICE = 20.0
COST_PCT = 0.24               # round-trip, statutory + slippage
SAMPLE_EVERY = 5              # entries every 5th day, keeps runtime sane


def load() -> tuple:
    from core.corporate_actions import is_tradeable_equity_symbol
    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    if not files:
        raise SystemExit("no bhavcopy archive")
    c: Dict = {}
    h: Dict = {}
    lo: Dict = {}
    v: Dict = {}
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["symbol", "date", "close", "high",
                                            "low", "volume"])
        except Exception:
            continue
        if d.empty:
            continue
        ts = pd.Timestamp(d["date"].iloc[0])
        d = d.drop_duplicates("symbol")
        d = d[d["symbol"].map(is_tradeable_equity_symbol)].set_index("symbol")
        c[ts], h[ts], lo[ts], v[ts] = (d["close"], d["high"],
                                       d["low"], d["volume"])
    return (pd.DataFrame(c).T.sort_index(), pd.DataFrame(h).T.sort_index(),
            pd.DataFrame(lo).T.sort_index(), pd.DataFrame(v).T.sort_index())


def simulate(close, high, low, vol, target_pct: float) -> dict:
    from core.corporate_actions import flag_panel

    ca = flag_panel(close)
    turnover = (close * vol).rolling(20).median()
    liq = (turnover.rank(axis=1, ascending=False) <= TOP_N_LIQUID) & (close > MIN_PRICE)

    cv, hv, lv = close.values, high.values, low.values
    cav, liqv = ca.values, liq.values
    n_rows, n_cols = cv.shape

    wins = losses = timeouts = ambiguous = 0
    pnl_opt, pnl_pess = [], []

    for i in range(20, n_rows - MAX_HOLD - 1, SAMPLE_EVERY):
        for j in range(n_cols):
            if not liqv[i, j] or cav[i, j]:
                continue
            entry = cv[i, j]
            if not np.isfinite(entry) or entry <= 0:
                continue
            tgt = entry * (1 + target_pct / 100.0)
            stp = entry * (1 - STOP_PCT / 100.0)

            hit = None
            amb = False
            for k in range(i + 1, min(i + 1 + MAX_HOLD, n_rows)):
                if cav[k, j]:
                    break                      # corporate action: abandon path
                hi, lo_ = hv[k, j], lv[k, j]
                if not (np.isfinite(hi) and np.isfinite(lo_)):
                    continue
                up, dn = hi >= tgt, lo_ <= stp
                if up and dn:
                    hit, amb = "both", True
                    break
                if up:
                    hit = "target"
                    break
                if dn:
                    hit = "stop"
                    break

            if hit is None:
                timeouts += 1
                continue
            if hit == "target":
                wins += 1
                pnl_opt.append(target_pct - COST_PCT)
                pnl_pess.append(target_pct - COST_PCT)
            elif hit == "stop":
                losses += 1
                pnl_opt.append(-STOP_PCT - COST_PCT)
                pnl_pess.append(-STOP_PCT - COST_PCT)
            else:
                ambiguous += 1
                pnl_opt.append(target_pct - COST_PCT)      # optimistic
                pnl_pess.append(-STOP_PCT - COST_PCT)      # pessimistic

    resolved = wins + losses + ambiguous
    if resolved == 0:
        return {}
    hr_opt = (wins + ambiguous) / resolved
    hr_pess = wins / resolved
    null = STOP_PCT / (STOP_PCT + target_pct)
    return {
        "target": target_pct, "n": resolved, "timeouts": timeouts,
        "null_hr": null, "hit_opt": hr_opt, "hit_pess": hr_pess,
        "net_opt": float(np.mean(pnl_opt)), "net_pess": float(np.mean(pnl_pess)),
        "ambiguous_pct": ambiguous / resolved * 100,
    }


def main() -> None:
    close, high, low, vol = load()
    print(f"panel: {close.shape[1]:,} symbols x {close.shape[0]:,} days")
    print(f"stop {STOP_PCT}%, max hold {MAX_HOLD} bars, cost {COST_PCT}% RT, "
          f"top {TOP_N_LIQUID} liquid, entries every {SAMPLE_EVERY}th day\n")

    hdr = (f"{'target%':>8s} {'null_hr':>8s} {'hit(opt)':>9s} {'hit(pess)':>10s} "
           f"{'net(opt)%':>10s} {'net(pess)%':>11s} {'n':>8s} {'amb%':>6s}")
    print(hdr); print("-" * len(hdr))
    rows = []
    for t in TARGETS:
        r = simulate(close, high, low, vol, t)
        if not r:
            continue
        rows.append(r)
        print(f"{r['target']:8.3f} {r['null_hr']*100:7.1f}% {r['hit_opt']*100:8.1f}% "
              f"{r['hit_pess']*100:9.1f}% {r['net_opt']:10.3f} {r['net_pess']:11.3f} "
              f"{r['n']:8,} {r['ambiguous_pct']:5.1f}")

    print("\nnull_hr is the driftless-walk hit rate S/(S+T) -- what the level")
    print("choice alone buys you. A real edge shows as hit > null, not hit high.")
    for r in rows:
        if abs(r["target"] - 4.286) < 0.01:
            edge_opt = (r["hit_opt"] - r["null_hr"]) * 100
            edge_pess = (r["hit_pess"] - r["null_hr"]) * 100
            print(f"\nAT THE 70% CONFIG (target 4.286%, stop 10%):")
            print(f"  measured hit rate : {r['hit_pess']*100:.1f}% (pess) .. "
                  f"{r['hit_opt']*100:.1f}% (opt)   vs null {r['null_hr']*100:.1f}%")
            print(f"  edge over null    : {edge_pess:+.2f} .. {edge_opt:+.2f} points")
            print(f"  net per trade     : {r['net_pess']:+.3f}% .. {r['net_opt']:+.3f}%")
            need = (STOP_PCT + COST_PCT) / (STOP_PCT + r["target"]) * 100
            print(f"  break-even hit rate needed: {need:.2f}%")


if __name__ == "__main__":
    main()
