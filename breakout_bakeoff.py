"""
breakout_bakeoff.py — FIVE "true breakout" variants, head-to-head, F&O universe.

User hypothesis: F&O stocks, long-only, capture TRUE breakouts (filtered by
volume / compression / retest) vs the naive breakouts already falsified
(breakout_5d 9-11% WR; 52wH anti-predictive in the committed backtest).

DESIGN (honest, comparable):
  * SAME exit for every variant (gap-honest simulate_exit from
    backtest_live_pipeline: SL = fill - 2*ATR14, TGT = fill + 3*ATR14 (1.5R),
    breakeven trail at 0.7R, 20-bar time stop). Differences between variants
    therefore reflect ENTRY QUALITY only — which is the question.
  * Entry at next-day OPEN with slippage. Net of config futures cost.
  * Fixed textbook params. NO tuning.
  * Trades split by entry date: IS = first 60% of the window, OOS = last 40%.
    OOS is the verdict; IS is shown for stability.
  * 5 variants tested -> expect ~0.25 false "passes" by chance; a single
    OOS-positive variant is a LEAD to re-test, not an edge.

Variants (long-only):
  A  b20        : close > prior 20-day high (naive Donchian — the control)
  B  b20_vol    : A + volume >= 1.5x 20d avg          ("true" filter: participation)
  C  squeeze    : 10-day range <= 2*ATR14 (compression) then close > range-high
                  with volume >= 1.3x                   ("true" filter: coiled spring)
  D  b52w       : close > prior 252-day high           (new 52-week high)
  E  retest     : 20d-high breakout in last 2-7 bars, price pulled back to
                  within 1.5% of the breakout level, today closes back above it
                  ("true" filter: breakout that HELD its retest)

Run:  python breakout_bakeoff.py --full --limit 120 --days 1460
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_live_pipeline import fetch_daily, simulate_exit, DEFAULT_UNIVERSE
try:
    import config
    COST_RT = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06)) / 100.0
except Exception:
    COST_RT = 0.0006

SLIP = 0.0005
ATR_SL_MULT, ATR_TGT_MULT = 2.0, 3.0     # 1.5R fixed
HOLD = 20
BE_TRAIL_R = 0.7
VARIANTS = ["b20", "b20_vol", "squeeze", "b52w", "retest"]


def _arrays(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    h, l, c, v = (df["high"], df["low"], df["close"], df["volume"])
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return {
        "c": c.values, "h": h.values, "l": l.values, "v": v.values,
        "h20": h.rolling(20).max().shift(1).values,
        "h252": h.rolling(252).max().shift(1).values,
        "v20": v.rolling(20).mean().shift(1).values,
        "atr": tr.rolling(14).mean().values,
        "r10h": h.rolling(10).max().shift(1).values,
        "r10l": l.rolling(10).min().shift(1).values,
    }


def _signal(var: str, A: Dict[str, np.ndarray], i: int) -> bool:
    c, v = A["c"], A["v"]
    if var == "b20":
        return i >= 21 and np.isfinite(A["h20"][i]) and c[i] > A["h20"][i]
    if var == "b20_vol":
        return (_signal("b20", A, i) and np.isfinite(A["v20"][i])
                and A["v20"][i] > 0 and v[i] >= 1.5 * A["v20"][i])
    if var == "squeeze":
        if i < 30 or not np.isfinite(A["atr"][i]) or A["atr"][i] <= 0:
            return False
        if not (np.isfinite(A["r10h"][i]) and np.isfinite(A["r10l"][i])):
            return False
        compressed = (A["r10h"][i] - A["r10l"][i]) <= 2.0 * A["atr"][i]
        vol_ok = (np.isfinite(A["v20"][i]) and A["v20"][i] > 0
                  and v[i] >= 1.3 * A["v20"][i])
        return compressed and c[i] > A["r10h"][i] and vol_ok
    if var == "b52w":
        return i >= 253 and np.isfinite(A["h252"][i]) and c[i] > A["h252"][i]
    if var == "retest":
        if i < 30:
            return False
        l_ = A["l"]
        for j in range(max(21, i - 7), i - 1):
            L = A["h20"][j]
            if not np.isfinite(L) or A["c"][j] <= L:
                continue                      # no breakout at j
            seg_lo = l_[j + 1:i + 1].min() if i > j else np.inf
            if seg_lo <= L * 1.015 and c[i] > L:
                return True                   # pulled back to level, held, resumed
        return False
    return False


def run_variant(var: str, udata: Dict[str, pd.DataFrame]) -> List[Dict]:
    trades: List[Dict] = []
    for sym, df in udata.items():
        A = _arrays(df)
        n = len(df)
        i = 30
        while i < n - 2:
            if not _signal(var, A, i):
                i += 1
                continue
            atr_v = A["atr"][i]
            if not np.isfinite(atr_v) or atr_v <= 0:
                i += 1
                continue
            fill_idx = i + 1
            entry = float(df["open"].iloc[fill_idx]) * (1 + SLIP)
            sl = entry - ATR_SL_MULT * atr_v
            tgt = entry + ATR_TGT_MULT * atr_v
            outcome, exit_raw, hold_bars, gapped = simulate_exit(
                df, fill_idx, entry, sl, tgt, "long",
                hold=HOLD, be_trail_r=BE_TRAIL_R)
            exit_fill = exit_raw * (1 - SLIP)
            pnl = (exit_fill - entry) / entry - COST_RT
            trades.append({
                "symbol": sym, "variant": var,
                "entry_date": df.index[fill_idx],
                "pnl": pnl, "outcome": outcome, "gapped": gapped,
                "R": (exit_fill - entry) / max(entry - sl, 1e-9),
            })
            i = fill_idx + max(hold_bars, 1)     # no overlapping trades per symbol
    return trades


def _stats(trades: List[Dict]) -> Dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "exp": 0.0, "avgR": 0.0}
    p = np.array([t["pnl"] for t in trades])
    w, l = p[p > 0], p[p < 0]
    pf = w.sum() / -l.sum() if l.size else float("inf")
    return {"n": len(p), "wr": 100 * len(w) / len(p), "pf": pf,
            "exp": float(p.mean()) * 100,
            "avgR": float(np.mean([t["R"] for t in trades]))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    if args.full:
        try:
            from core.universe import FO_UNIVERSE
            syms = list(dict.fromkeys(FO_UNIVERSE))   # F&O ONLY — by design
        except Exception:
            syms = DEFAULT_UNIVERSE
    else:
        syms = DEFAULT_UNIVERSE
    syms = syms[:args.limit] if args.limit else syms

    print(f"[BRK] F&O-only universe: {len(syms)} names ({args.days}d) ...")
    udata: Dict[str, pd.DataFrame] = {}
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is not None and len(df) > 300:
            udata[s] = df[~df.index.duplicated(keep="last")].sort_index()
    if len(udata) < 8:
        print("[BRK] FATAL: not enough data."); return 1

    all_dates = sorted(set().union(*[set(df.index) for df in udata.values()]))
    cutoff = all_dates[int(len(all_dates) * 0.60)]
    nifty = fetch_daily("NIFTY", args.days)
    nf_oos_ann = 0.0
    if nifty is not None:
        nf = nifty[~nifty.index.duplicated(keep="last")].sort_index()["close"]
        nf_oos = nf[nf.index >= cutoff]
        if len(nf_oos) > 30:
            yrs = (nf_oos.index[-1] - nf_oos.index[0]).days / 365.0
            nf_oos_ann = ((nf_oos.iloc[-1] / nf_oos.iloc[0]) ** (1 / max(yrs, 0.1)) - 1) * 100
    print(f"[BRK] {len(udata)} names. IS < {cutoff.date()} <= OOS. "
          f"NIFTY OOS ann ~ {nf_oos_ann:+.1f}%")

    print("\n" + "=" * 88)
    print("  TRUE-BREAKOUT BAKE-OFF — long-only, F&O universe, SAME gap-honest exit")
    print(f"  (SL 2*ATR, TGT 3*ATR=1.5R, BE-trail 0.7R, 20-bar stop, net {COST_RT*100:.2f}% cost)")
    print("=" * 88)
    print(f"  {'variant':9} | {'IS n':>5} {'IS WR%':>6} {'IS PF':>6} | "
          f"{'OOS n':>5} {'OOS WR%':>6} {'OOS PF':>6} {'OOS exp%':>8} {'avgR':>6}  verdict")
    print("-" * 88)
    results = {}
    for var in VARIANTS:
        trades = run_variant(var, udata)
        is_t = [t for t in trades if t["entry_date"] < cutoff]
        oos_t = [t for t in trades if t["entry_date"] >= cutoff]
        si, so = _stats(is_t), _stats(oos_t)
        results[var] = (si, so)
        passes = so["n"] >= 30 and so["pf"] > 1.2 and so["exp"] > 0
        verdict = "PASS (lead)" if passes else (
            "positive-thin" if so["exp"] > 0 else "NEGATIVE")
        pf_i = "inf" if si["pf"] == float("inf") else f"{si['pf']:.2f}"
        pf_o = "inf" if so["pf"] == float("inf") else f"{so['pf']:.2f}"
        print(f"  {var:9} | {si['n']:>5} {si['wr']:>6.1f} {pf_i:>6} | "
              f"{so['n']:>5} {so['wr']:>6.1f} {pf_o:>6} {so['exp']:>+8.3f} "
              f"{so['avgR']:>+6.2f}  {verdict}")
    print("-" * 88)
    print(f"  Benchmark: holding NIFTY over the same OOS window ~ {nf_oos_ann:+.1f}%/yr")
    print("=" * 88)

    # honest summary
    passing = [v for v in VARIANTS
               if results[v][1]["n"] >= 30 and results[v][1]["pf"] > 1.2
               and results[v][1]["exp"] > 0]
    print("\n  VERDICT (multiple-testing aware: 5 variants -> ~0.25 false passes expected)")
    if not passing:
        best = max(VARIANTS, key=lambda v: results[v][1]["exp"])
        so = results[best][1]
        print("  NO breakout variant clears the bar (OOS n>=30, PF>1.2, exp>0) net of")
        print("  costs. 'True breakout' filters did not rescue the breakout family —")
        print(f"  least-bad was '{best}' (OOS PF "
              f"{'inf' if so['pf']==float('inf') else round(so['pf'],2)}, exp {so['exp']:+.3f}%).")
        print("  Consistent with the committed evidence (breakout_5d 9-11% WR; 52wH")
        print("  anti-predictive). Recommendation stands: do NOT trade breakouts with")
        print("  real money on this universe/timeframe.")
    else:
        for v in passing:
            si, so = results[v]
            stable = si["exp"] > 0
            print(f"  '{v}' PASSES OOS (n={so['n']}, PF {so['pf']:.2f}, exp {so['exp']:+.3f}%)"
                  f" | IS {'also positive — stable' if stable else 'NEGATIVE — unstable, suspect'}")
        print("  Any pass is a LEAD, not an edge: re-test on a point-in-time universe")
        print("  and a different regime, then paper-trade forward before ANY capital.")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
