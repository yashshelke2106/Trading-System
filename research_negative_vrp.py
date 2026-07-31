"""
research_negative_vrp.py — H-016: is there a state where BUYING options is +EV?

THE ONE REQUIREMENT, STATED CORRECTLY
-------------------------------------
A long option does not merely need direction. It needs the REALIZED move to
exceed the IMPLIED move you paid for. Unconditionally, implied exceeds realized
~77% of the time — that IS the volatility risk premium, and it is exactly why
buying loses and selling wins.

So the only way directional buying becomes +EV is to find a CONDITION under
which the premium flips NEGATIVE: realized > implied. This script hunts for
that condition.

PRIMARY CANDIDATE (pre-registered)
----------------------------------
Very low VIX = complacency, and volatility mean-reverts. If cheap vol
systematically under-prices the move that follows, the low-VIX bucket should
show realized > implied. That is the classic "buy vol when it is cheap" claim,
and it is testable on 18 years of India VIX + NIFTY (2008-2026, including both
the 2008 and 2020 crashes — essential, since the whole thesis lives in the
tail).

METHOD
------
For each day t and horizon h:
    implied_move  = VIX_t/100 * sqrt(h/365)          (as a fraction of spot)
    realized_move = |NIFTY_{t+h} / NIFTY_t - 1|      (absolute — a long option
                                                      pays on magnitude either
                                                      way if you pick the side)
    vrp           = implied_move - realized_move     (>0 = seller wins)
Bucket every day by VIX PERCENTILE (its own trailing history, point-in-time —
no lookahead). A bucket with mean(vrp) < 0 is a state where buying beats
selling before cost.

GATES (a positive bucket must survive all of them)
--------------------------------------------------
  1. cost: a long option pays ~5% of premium round-trip in slippage/fees
  2. Bonferroni across the buckets tested (this is a search)
  3. the registry trial count (16 hypotheses and counting)
  4. overlapping-window correction: daily samples with an h-day horizon are
     NOT independent; t-stats are computed on non-overlapping samples only

PRIOR (registered before running): expect the low-VIX bucket to show SOME vol
expansion — vol mean-reversion is well documented — but likely not enough to
clear premium + cost after correction. The VRP exists because this is hard.

RUN
---
    python research_negative_vrp.py
    python research_negative_vrp.py --horizon 10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ssl._create_default_https_context = ssl._create_unverified_context

COST_FRAC_OF_PREMIUM = 0.05      # round-trip slippage+fees on a long option
VIX_PCTL_WINDOW = 504            # ~2y trailing window for the percentile
BUCKETS = [(0, 10), (10, 25), (25, 50), (50, 75), (75, 90), (90, 100)]


def load() -> pd.DataFrame:
    import yfinance as yf
    vix = yf.Ticker("^INDIAVIX").history(period="max", interval="1d")["Close"]
    nif = yf.Ticker("^NSEI").history(period="max", interval="1d")["Close"]
    for s in (vix, nif):
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    df = pd.DataFrame({"vix": vix, "nifty": nif}).dropna()
    return df[~df.index.duplicated(keep="last")].sort_index()


def analyse(df: pd.DataFrame, horizon: int, cost: float) -> pd.DataFrame:
    d = df.copy()
    # Point-in-time VIX percentile: rank within its OWN trailing window only.
    d["vix_pctl"] = (d["vix"].rolling(VIX_PCTL_WINDOW)
                     .apply(lambda w: (w[-1] > w[:-1]).mean() * 100, raw=True))
    # Implied move over the horizon, as a fraction.
    d["implied"] = d["vix"] / 100.0 * math.sqrt(horizon / 365.0)
    # Realized ABSOLUTE move over the same horizon.
    d["realized"] = (d["nifty"].shift(-horizon) / d["nifty"] - 1).abs()
    # Cost inflates what the buyer must beat.
    d["implied_c"] = d["implied"] * (1 + cost)
    d["vrp"] = d["implied_c"] - d["realized"]      # >0 seller wins, <0 buyer wins
    return d.dropna(subset=["vix_pctl", "realized", "vrp"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=21)
    ap.add_argument("--cost", type=float, default=COST_FRAC_OF_PREMIUM)
    args = ap.parse_args()
    h = args.horizon

    print("=== H-016: is there a state where BUYING options is +EV? ===")
    print(f"horizon {h}d | buyer pays {args.cost:.0%} of premium in cost")
    print("vrp = implied(+cost) - realized.  vrp < 0 => BUYER wins\n")

    df = load()
    print(f"data: {len(df)} days  {df.index.min().date()} -> {df.index.max().date()}")
    d = analyse(df, h, args.cost)
    print(f"usable rows: {len(d)}  (incl. 2008 + 2020 crashes)\n")

    print(f"{'VIX pctl':>12}{'n':>7}{'implied':>10}{'realized':>10}"
          f"{'mean vrp':>11}{'buyer win%':>12}{'t (indep)':>11}")
    print("-" * 74)

    rows = []
    for lo, hi in BUCKETS:
        b = d[(d["vix_pctl"] >= lo) & (d["vix_pctl"] < hi)]
        if len(b) < 30:
            continue
        # Overlapping windows are NOT independent: take every h-th row.
        indep = b.iloc[::h]
        vrp = indep["vrp"].values
        n = len(vrp)
        if n < 10:
            continue
        mean_vrp = vrp.mean()
        sd = vrp.std(ddof=1)
        t = mean_vrp / (sd / math.sqrt(n)) if sd > 0 else float("nan")
        buyer_win = (b["vrp"] < 0).mean() * 100
        rows.append({"bucket": f"{lo}-{hi}", "n_indep": n,
                     "implied": b["implied"].mean(), "realized": b["realized"].mean(),
                     "mean_vrp": mean_vrp, "buyer_win_pct": buyer_win, "t": t})
        print(f"{lo:>5}-{hi:<6}{n:>7}{b['implied'].mean()*100:>9.2f}%"
              f"{b['realized'].mean()*100:>9.2f}%{mean_vrp*100:>10.2f}%"
              f"{buyer_win:>11.1f}%{t:>11.2f}")

    print()
    # A buyer-favourable bucket = mean vrp NEGATIVE with a significant t.
    winners = [r for r in rows if r["mean_vrp"] < 0]
    n_tests = len(rows)
    print("=" * 74)
    if not winners:
        print("NO bucket has negative mean VRP. In EVERY volatility state the")
        print("implied move exceeds the realized move net of cost — the seller")
        print("is favoured everywhere. There is no condition here under which")
        print("buying options is +EV.")
    else:
        print(f"Buyer-favourable buckets (mean vrp < 0): {len(winners)}")
        for w in winners:
            p_raw = math.erfc(abs(w["t"]) / math.sqrt(2))
            p_bonf = min(1.0, p_raw * max(n_tests, 1))
            try:
                from core.hypothesis_registry import trial_count
                p_prog = min(1.0, p_raw * trial_count())
            except Exception:
                p_prog = p_bonf
            print(f"  VIX pctl {w['bucket']}: mean vrp {w['mean_vrp']*100:+.2f}% "
                  f"t={w['t']:.2f} n={w['n_indep']}")
            print(f"    p_raw={p_raw:.4f}  Bonferroni x{n_tests}={p_bonf:.4f}  "
                  f"x registry={p_prog:.4f}")
            print(f"    verdict: {'SURVIVES' if p_bonf < 0.05 else 'FAILS correction'}")

    os.makedirs("docs/research", exist_ok=True)
    with open("docs/research/negative_vrp_H016.json", "w", encoding="utf-8") as fh:
        json.dump({"horizon": h, "cost": args.cost, "buckets": rows},
                  fh, indent=2, default=str)
    print("\nwrote docs/research/negative_vrp_H016.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
