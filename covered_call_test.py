"""
covered_call_test.py — honest validation of the covered-call income sleeve:
hold NIFTY (ETF proxy) + sell a monthly call against it. The "safe active"
candidate from the bake-off — structurally cannot blow up (worst case: upside
capped), unlike the directional F&O trades that produced real losses.

WHAT IT TESTS: does the call premium collected exceed the upside surrendered,
net of costs, vs simply holding NIFTY? (The classic BXM-style question.)

METHOD (same approach as research_iron_condor.py):
  * Every 21 trading days: sell 1 call, strike = S*(1+OTM), priced with
    Black-Scholes, sigma = India VIX, T = 21/252, r = 6.5%. Hold to expiry,
    settle against where NIFTY actually went. Costs = 5% of premium.
  * Variants (fixed a priori, textbook): ATM (0%), 2.5% OTM, 5% OTM.
  * IS = first 60%, OOS = last 40%. Benchmark = buy & hold NIFTY.

HONESTY CAVEATS (read before believing a pass):
  * India VIX as the call IV OVERSTATES premium income — index OTM calls trade
    below VIX-ish ATM vol (skew). So results are OPTIMISTIC on income. A FAIL
    here is decisive; a PASS must be discounted and forward-paper-tested.
  * No early management, no dividends, monthly only. Lot-size lumpiness ignored.

Run:  python covered_call_test.py --days 2600
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import warnings
from typing import Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_live_pipeline import fetch_daily

RISK_FREE = 0.065
STEP = 21                      # monthly cycle in trading days
COST_FRAC = 0.05               # 5% of premium (slippage + fees), as in condor harness
OTM_VARIANTS = [0.0, 0.025, 0.05]
IS_FRAC = 0.60


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, sigma: float, T: float, r: float = RISK_FREE) -> float:
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def metrics(rets: List[float]) -> Dict:
    a = np.array(rets, dtype=float)
    if len(a) < 4 or a.std() == 0:
        return {"n": len(a), "ann": 0.0, "sharpe": 0.0, "mdd": 0.0,
                "worst": 0.0, "pos": 0.0}
    ann = float(a.mean() * 12 * 100)
    sharpe = float(a.mean() / a.std() * math.sqrt(12))
    eq = np.cumprod(1 + a)
    peak = np.maximum.accumulate(eq)
    mdd = float(((eq - peak) / peak).min() * 100)
    return {"n": len(a), "ann": ann, "sharpe": sharpe, "mdd": mdd,
            "worst": float(a.min() * 100), "pos": float((a > 0).mean() * 100)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2600)
    args = ap.parse_args()

    print(f"[CC] fetching NIFTY + India VIX ({args.days}d) ...")
    nf_df = fetch_daily("NIFTY", args.days)
    vx_df = fetch_daily("INDIAVIX", args.days)
    if nf_df is None or vx_df is None:
        print("[CC] FATAL: need NIFTY and INDIAVIX from Dhan."); return 1
    nf = nf_df[~nf_df.index.duplicated(keep="last")].sort_index()["close"]
    vx = vx_df[~vx_df.index.duplicated(keep="last")].sort_index()["close"]
    common = nf.index.intersection(vx.index)
    nf, vx = nf.reindex(common), vx.reindex(common)
    print(f"[CC] aligned {len(common)} days  {common[0].date()} -> {common[-1].date()}")
    if len(common) < 400:
        print("[CC] not enough history."); return 1

    # build monthly observations
    cycles = []
    for t in range(0, len(common) - STEP, STEP):
        S0 = float(nf.iloc[t]); ST = float(nf.iloc[t + STEP])
        iv = float(vx.iloc[t]) / 100.0
        if S0 <= 0 or iv <= 0:
            continue
        cycles.append((common[t], S0, ST, iv))
    cut = int(len(cycles) * IS_FRAC)
    cut_date = cycles[cut][0]
    print(f"[CC] {len(cycles)} monthly cycles. IS < {cut_date.date()} <= OOS\n")

    bh = [(ST - S0) / S0 for _, S0, ST, _ in cycles]

    print("=" * 92)
    print("  COVERED CALL on NIFTY — monthly, BS-priced @ India VIX (income OPTIMISTIC: skew)")
    print(f"  cost {COST_FRAC:.0%} of premium | benchmark = buy & hold NIFTY")
    print("=" * 92)
    print(f"  {'strategy':16} | {'ann%':>6} {'Sharpe':>7} {'maxDD%':>7} {'worst mo':>8} "
          f"{'%+mo':>5} | {'IS Shp':>6} {'OOS Shp':>7} | {'called%':>7} {'prem/mo':>7}")
    print("-" * 92)

    bh_m = metrics(bh)
    bh_is, bh_oos = metrics(bh[:cut]), metrics(bh[cut:])
    print(f"  {'Buy&hold NIFTY':16} | {bh_m['ann']:>+6.1f} {bh_m['sharpe']:>7.2f} "
          f"{bh_m['mdd']:>7.1f} {bh_m['worst']:>+8.1f} {bh_m['pos']:>5.0f} | "
          f"{bh_is['sharpe']:>6.2f} {bh_oos['sharpe']:>7.2f} | {'—':>7} {'—':>7}")

    results = {}
    for otm in OTM_VARIANTS:
        rets, called, prem_yield = [], 0, []
        for _, S0, ST, iv in cycles:
            K = S0 * (1 + otm)
            prem = bs_call(S0, K, iv, STEP / 252.0) * (1 - COST_FRAC)
            payoff = max(ST - K, 0.0)
            rets.append((ST - S0) / S0 + (prem - payoff) / S0)
            prem_yield.append(prem / S0 * 100)
            if payoff > 0:
                called += 1
        m = metrics(rets)
        mi, mo = metrics(rets[:cut]), metrics(rets[cut:])
        results[otm] = (m, mi, mo)
        name = "ATM (0%)" if otm == 0 else f"{otm*100:.1f}% OTM"
        print(f"  {'CC ' + name:16} | {m['ann']:>+6.1f} {m['sharpe']:>7.2f} "
              f"{m['mdd']:>7.1f} {m['worst']:>+8.1f} {m['pos']:>5.0f} | "
              f"{mi['sharpe']:>6.2f} {mo['sharpe']:>7.2f} | "
              f"{100*called/len(cycles):>6.0f}% {np.mean(prem_yield):>6.2f}%")
    print("-" * 92)

    # verdict: CC must beat B&H Sharpe in BOTH halves to count (premium income
    # is optimistic, so demand stability, not just a full-period win)
    print("\n  VERDICT")
    winners = []
    for otm, (m, mi, mo) in results.items():
        if (m["sharpe"] > bh_m["sharpe"] and mi["sharpe"] > bh_is["sharpe"]
                and mo["sharpe"] > bh_oos["sharpe"]):
            winners.append((otm, m))
    if winners:
        best = max(winners, key=lambda x: x[1]["sharpe"])
        name = "ATM" if best[0] == 0 else f"{best[0]*100:.1f}% OTM"
        print(f"  CC {name} beats buy&hold on risk-adjusted return in BOTH halves "
              f"(Sharpe {best[1]['sharpe']:.2f} vs {bh_m['sharpe']:.2f}).")
        print("  Remember the income is OPTIMISTICALLY priced (VIX vs call skew):")
        print("  treat as a PAPER candidate — sell real (paper) calls forward for 2-3")
        print("  months and compare realized premium to the model before any capital.")
    else:
        print("  NO covered-call variant beats buy&hold in BOTH halves — and the")
        print("  premium income here is already optimistic. On this data the overlay")
        print("  does not add risk-adjusted value: just hold the index (or it's close")
        print("  to a wash — in which case prefer the simpler thing: the index).")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
