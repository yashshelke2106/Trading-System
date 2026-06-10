"""
validate_meanrev.py — PROPER validation of the one lead from edge_research.py
(Connors RSI-2 short-term mean reversion). This is "how you do this honestly".

Upgrades over the edge_research.py H3 quick-check:
  * REAL OHLC: entry filled at the NEXT day's actual OPEN (not close≈open).
  * Gap-honest exits at the next open.
  * COST-SENSITIVITY SWEEP — the decisive test. MR is high-turnover; a tiny
    per-trade edge is eaten by costs. We show PF/expectancy at 0 / 0.06 / 0.12 /
    0.20 / 0.30% round-trip so you can see exactly where (and if) it dies.
  * Temporal H1/H2 split at a realistic cost (held-out robustness).
  * Max adverse excursion (how much heat each trade took) for risk sizing.

FIXED textbook params — NO optimization, NO peeking:
  RSI length 2, entry RSI<10, regime filter close>SMA200, exit close>SMA5,
  max hold 10 days, LONG ONLY.

Honesty caveats printed at the end: survivorship (today's names = optimistic),
multiple-testing (this was 1 of 3 hypotheses — pursuing the winner risks
chasing noise; broader universe + real fills here is a partial independent
re-test), single market regime.

Run:  python validate_meanrev.py --full --days 1095
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_live_pipeline import fetch_daily, DEFAULT_UNIVERSE

# FIXED params (Connors textbook). Do not tune.
RSI_LEN, RSI_ENTRY = 2, 10
REGIME_SMA, EXIT_SMA, MAX_HOLD = 200, 5, 10
COST_LEVELS = [0.0, 0.0006, 0.0012, 0.0020, 0.0030]   # round-trip fractions
REALISTIC_COST = 0.0012   # 0.12% RT for H1/H2 split (liquid futures+slippage; midcaps worse)


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def simulate_symbol(df: pd.DataFrame) -> List[Dict]:
    """RSI-2 mean reversion, real OHLC, entry/exit at the OPEN (no slip here —
    cost is swept separately). Returns list of trade dicts with raw_gross."""
    c, o, l = df["close"], df["open"], df["low"]
    n = len(c)
    if n < REGIME_SMA + 15:
        return []
    sma200 = c.rolling(REGIME_SMA).mean()
    sma5 = c.rolling(EXIT_SMA).mean()
    rsi2 = _rsi(c, RSI_LEN)
    out: List[Dict] = []
    i = REGIME_SMA + 5
    while i < n - 1:
        if rsi2.iloc[i] < RSI_ENTRY and c.iloc[i] > sma200.iloc[i]:
            fill = i + 1
            entry = float(o.iloc[fill])
            if entry <= 0:
                i += 1
                continue
            exit_px = None
            exit_idx = None
            mae = 0.0
            for k in range(fill, min(fill + MAX_HOLD, n)):
                mae = min(mae, (float(l.iloc[k]) - entry) / entry)
                if c.iloc[k] > sma5.iloc[k]:                 # exit signal -> next open
                    xi = k + 1
                    exit_px = float(o.iloc[xi]) if xi < n else float(c.iloc[k])
                    exit_idx = xi if xi < n else k
                    break
            if exit_px is None:                              # time stop
                exit_idx = min(fill + MAX_HOLD, n - 1)
                exit_px = float(c.iloc[exit_idx])
            raw = (exit_px - entry) / entry
            out.append({"date": df.index[fill], "raw": raw, "mae": mae,
                        "hold": exit_idx - fill})
            i = exit_idx + 1                                  # no overlap per symbol
        else:
            i += 1
    return out


def _stats(rets: np.ndarray) -> Dict:
    if len(rets) == 0:
        return {"n": 0, "wr": 0, "pf": 0, "exp": 0}
    w, l = rets[rets > 0], rets[rets < 0]
    pf = w.sum() / -l.sum() if l.size else float("inf")
    return {"n": len(rets), "wr": 100 * len(w) / len(rets),
            "pf": pf, "exp": float(np.mean(rets)) * 100}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    if args.full:
        try:
            from core.universe import FO_UNIVERSE
            syms = list(dict.fromkeys(FO_UNIVERSE))
        except Exception:
            syms = DEFAULT_UNIVERSE
    else:
        syms = DEFAULT_UNIVERSE
    syms = syms[:args.limit] if args.limit else syms

    print("[MR] !!! survivor-biased universe (today's names) = OPTIMISTIC. And this")
    print("[MR]     was 1 of 3 hypotheses — treat a pass as 'worth a PIT re-test', not truth.")
    print(f"[MR] fetching {len(syms)} names ({args.days}d) ...")
    trades: List[Dict] = []
    got = 0
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is None or len(df) < REGIME_SMA + 15:
            continue
        got += 1
        trades.extend(simulate_symbol(df))
    if not trades:
        print("[MR] FATAL: no trades (Dhan unreachable or too little data).")
        return 1

    trades.sort(key=lambda t: t["date"])
    raw = np.array([t["raw"] for t in trades])
    holds = np.array([t["hold"] for t in trades])
    maes = np.array([t["mae"] for t in trades])
    print(f"[MR] {got} names, {len(raw)} trades, "
          f"{trades[0]['date'].date()} -> {trades[-1]['date'].date()}")
    print(f"[MR] avg hold {holds.mean():.1f}d   median MAE {np.median(maes)*100:.2f}%   "
          f"worst MAE {maes.min()*100:.1f}%")

    print("\n" + "=" * 66)
    print("  COST SENSITIVITY  (round-trip cost subtracted per trade)")
    print("=" * 66)
    print(f"  {'cost RT':>8}  {'WR%':>6}  {'PF':>6}  {'exp%/trade':>11}  {'ann% (approx)':>13}")
    trades_per_yr = len(raw) / (args.days / 365.0)
    for cst in COST_LEVELS:
        net = raw - cst
        s = _stats(net)
        pf = s["pf"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        # crude annualised: exp/trade * trades/yr (each trade ~ independent slug)
        ann = s["exp"] * (trades_per_yr / max(got, 1))   # per-name pacing ~ portfolio
        print(f"  {cst*100:>7.2f}%  {s['wr']:>6.1f}  {pf_s:>6}  {s['exp']:>+10.3f}%  "
              f"{ann:>+12.1f}%")

    # H1/H2 at realistic cost
    half = len(raw) // 2
    net = raw - REALISTIC_COST
    h1, h2 = _stats(net[:half]), _stats(net[half:])
    full = _stats(net)
    print("\n" + "=" * 66)
    print(f"  HELD-OUT SPLIT  @ realistic {REALISTIC_COST*100:.2f}% round-trip")
    print("=" * 66)
    print(f"  FULL: n={full['n']}  WR {full['wr']:.1f}%  PF {full['pf']:.2f}  exp {full['exp']:+.3f}%")
    print(f"  H1  : n={h1['n']}  WR {h1['wr']:.1f}%  PF {h1['pf']:.2f}  exp {h1['exp']:+.3f}%")
    print(f"  H2  : n={h2['n']}  WR {h2['wr']:.1f}%  PF {h2['pf']:.2f}  exp {h2['exp']:+.3f}%  <- held-out")

    print("\n" + "=" * 66)
    print("  VERDICT")
    print("=" * 66)
    survives = (full["pf"] > 1.2 and full["exp"] > 0 and h2["pf"] > 1.1 and h2["exp"] > 0)
    if survives:
        print("  PASSES this honest in-sample-ish test at realistic cost. NOT a green")
        print("  light: re-run on a POINT-IN-TIME (non-survivor) universe across a")
        print("  drawdown regime, with real F&O lot costs, before risking capital.")
    else:
        # find the cost where it breaks
        breakeven = None
        for cst in COST_LEVELS:
            if _stats(raw - cst)["exp"] <= 0:
                breakeven = cst
                break
        msg = f"dies by {breakeven*100:.2f}% RT cost" if breakeven is not None else "thin"
        print(f"  DOES NOT survive honest costs ({msg}). The lead was likely noise /")
        print(f"  multiple-testing. Mean reversion is not a deployable edge on this data.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
