"""
Cross-sectional (relative-strength) momentum — Jegadeesh-Titman, the last distinct
edge candidate for NSE F&O.  (User: "run it for closure, go in honest.")

NOT the same as the trend-following / breakout tests: those were TIME-SERIES
(does a stock's own past predict its own future). This is CROSS-SECTIONAL: each
month rank ALL stocks by trailing return, LONG the top decile, SHORT the bottom,
rebalance monthly. The most-replicated equity anomaly there is — and also the
most decayed and most crash-prone.

PRE-REGISTERED (chosen before looking):
  SIGNAL   : 12-1 momentum — formation return over months [i-12, i-1], i.e.
             P[i-1]/P[i-12]-1. The most recent month is SKIPPED (1-month reversal
             is a known contaminant). No lookahead: ranking uses P[i-1] & earlier,
             entry at P[i] (known at month-end i), exit P[i+1].
  PORTFOLIO: long top decile, short bottom decile, equal-weight. Also report
             LONG-ONLY (what you could actually trade) and the NIFTY benchmark.
  REBAL    : monthly. COSTS: round-trip on actual name turnover each month.
  JUDGE    : monthly-return t-stat, annualised, Sharpe, MAX DRAWDOWN, WORST month
             (momentum-crash tail). IS/OOS split by date.
  NULL     : random baskets (same sizes) each month -> does the momentum RANK beat
             random selection? p over draws.

HONEST LIMITS (state up front, don't bury):
  * SURVIVORSHIP: the universe is TODAY's F&O list. Names that delisted/blew up
    are absent -> momentum is INFLATED here (it dropped the losers for us). Same
    unfixable issue as the pairs study (no historical F&O membership data). A
    positive result is therefore an UPPER bound, not a realizable one.
  * Monthly decile turnover is high -> costs matter a lot; we charge them.
  * Momentum crashes (2009-style) give back years in months — watch worst month.

Run:
    python xsect_momentum_study.py                 # full 153 universe
    python xsect_momentum_study.py --top100
    python xsect_momentum_study.py --form 6        # 6-1 momentum
"""
from __future__ import annotations

import argparse, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import warnings; warnings.filterwarnings("ignore")
import logging
for n in ("core.api_dhan", "core.bar_cache"):
    logging.getLogger(n).setLevel(logging.CRITICAL)
import numpy as np
import pandas as pd

ANN      = np.sqrt(12)
COST_RT  = 0.0020
RNG      = np.random.default_rng(17)


def stats(series: np.ndarray, label: str, cost_series: np.ndarray | None = None):
    x = series[np.isfinite(series)]
    if len(x) < 6:
        print(f"  {label:20} n={len(x)} (too few)"); return None
    mean = x.mean()
    sd = x.std(ddof=1)
    t = mean / (sd / np.sqrt(len(x))) if sd > 0 else 0.0
    sharpe = (mean / sd * ANN) if sd > 0 else 0.0
    ann = (1 + x).prod() ** (12 / len(x)) - 1
    eq = np.cumprod(1 + x); peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min()
    print(f"  {label:20} n={len(x):3d}  mean {mean*100:+.2f}%/mo  t={t:+.2f}  "
          f"ann {ann*100:+.1f}%  Sharpe {sharpe:+.2f}  maxDD {mdd*100:.0f}%  "
          f"worst {x.min()*100:.0f}%")
    return {"mean": mean, "t": t, "sharpe": sharpe, "ann": ann, "mdd": mdd, "n": len(x)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top100", action="store_true")
    ap.add_argument("--days", type=int, default=2950)
    ap.add_argument("--form", type=int, default=12, help="formation months (12-1 default)")
    ap.add_argument("--decile", type=float, default=0.10)
    ap.add_argument("--perms", type=int, default=1000)
    args = ap.parse_args()
    J = args.form

    from core.bar_cache import cached_daily
    from core.universe import FO_UNIVERSE, TOP100_FO
    universe = TOP100_FO if args.top100 else FO_UNIVERSE

    print(f"[XS] building monthly price matrix, {len(universe)} symbols + NIFTY ...")
    monthly = {}
    for sym in universe + ["NIFTY"]:
        try:
            df = cached_daily(sym, args.days)
            if df is None or len(df) < 300:
                continue
            df = df[~df.index.duplicated(keep="last")].sort_index()
            monthly[sym] = df["close"].resample("ME").last()
        except Exception:
            continue
    P = pd.DataFrame(monthly).sort_index()
    nifty = P.pop("NIFTY") if "NIFTY" in P else None
    P = P.dropna(how="all")
    months = P.index
    print(f"[XS] {P.shape[1]} symbols, {len(months)} months "
          f"{months[0].date()} → {months[-1].date()}  (12-{1} = {J}-1 momentum)\n")
    if len(months) < J + 6:
        print("[XS] not enough months."); return

    # build monthly portfolio returns
    rows = []                       # (date, ls, longonly, mkt, turnover)
    prev_long = set(); prev_short = set()
    for i in range(J, len(months) - 1):
        form = P.iloc[i - 1] / P.iloc[i - J] - 1.0          # [i-J, i-1] formation
        hold = P.iloc[i + 1] / P.iloc[i] - 1.0               # next-month hold
        valid = form.notna() & hold.notna() & np.isfinite(P.iloc[i])
        f = form[valid]; hr = hold[valid]
        if len(f) < 20:
            continue
        k = max(3, int(len(f) * args.decile))
        order = f.sort_values()
        longs = set(order.index[-k:]); shorts = set(order.index[:k])
        long_ret = hr[list(longs)].mean()
        short_ret = hr[list(shorts)].mean()
        ls = long_ret - short_ret
        turn = (len(longs - prev_long) + len(shorts - prev_short)) / max(1, (len(longs)+len(shorts)))
        prev_long, prev_short = longs, shorts
        mkt = (nifty.iloc[i + 1] / nifty.iloc[i] - 1.0) if nifty is not None else np.nan
        rows.append((months[i], ls, long_ret, mkt, turn, k, list(f.index), hr.values))

    D = pd.DataFrame(rows, columns=["date","ls","longonly","mkt","turn","k","names","holds"])
    split = D["date"].quantile(0.70)
    IS = D[D.date < split]; OOS = D[D.date >= split]
    # cost drag: round-trip on turned-over names, both legs
    ls_cost  = D["ls"].values        - D["turn"].values * COST_RT * 2
    lo_cost  = D["longonly"].values  - D["turn"].values * COST_RT

    print("=" * 78)
    print(f"  CROSS-SECTIONAL MOMENTUM ({J}-1)  decile k≈{int(D['k'].mean())}  "
          f"OOS split {str(split)[:10]}  avg turnover {D['turn'].mean()*100:.0f}%/mo")
    print("=" * 78)
    print("\n  GROSS (no costs):")
    stats(D["ls"].values, "L/S full")
    stats(IS["ls"].values, "L/S IS")
    stats(OOS["ls"].values, "L/S OOS")
    stats(D["longonly"].values, "Long-only full")
    stats(D["mkt"].values, "NIFTY benchmark")
    print("\n  NET (turnover costs applied):")
    r_ls  = stats(ls_cost, "L/S NET full")
    stats(ls_cost[D.date.values >= np.datetime64(split)], "L/S NET OOS")
    r_lo  = stats(lo_cost, "Long-only NET")
    excess = lo_cost - D["mkt"].values
    stats(excess, "Long-only − NIFTY")

    # ── MATCHED NULL: random baskets, same sizes ─────────────────────────────
    print("\n" + "-" * 78)
    print(f"  MATCHED NULL — random long/short baskets, same sizes ({args.perms} draws)")
    print("-" * 78)
    real_sharpe = r_ls["sharpe"] if r_ls else 0.0
    null_sharpe = []
    holds_by_month = [(r.holds, r.k, r.turn) for r in D.itertuples()]
    for _ in range(args.perms):
        ser = []
        for holds, k, turn in holds_by_month:
            if len(holds) < 2 * k:
                continue
            pick = RNG.permutation(len(holds))
            rl = holds[pick[:k]].mean(); rs = holds[pick[k:2*k]].mean()
            ser.append((rl - rs) - turn * COST_RT * 2)
        ser = np.array(ser)
        if len(ser) >= 6 and ser.std() > 0:
            null_sharpe.append(ser.mean() / ser.std(ddof=1) * ANN)
    null_sharpe = np.array(null_sharpe)
    p = (np.sum(null_sharpe >= real_sharpe) + 1) / (len(null_sharpe) + 1)
    print(f"  real L/S NET Sharpe {real_sharpe:+.2f}   null mean {null_sharpe.mean():+.2f} "
          f"(p95 {np.percentile(null_sharpe,95):+.2f})   p={p:.3f}")

    # ── DECISIVE: LONG-ONLY null — does momentum RANK beat a RANDOM long basket? ──
    # (long-only beat NIFTY, but so might ANY equal-weight long basket of survivor
    #  F&O names in a bull market. Hold beta/survivorship/equal-weight constant by
    #  comparing the momentum long decile to RANDOM long deciles, same size/months.)
    oos_mask = D.date.values >= np.datetime64(split)
    months_meta = list(zip(holds_by_month, oos_mask))
    real_lo_full = r_lo["mean"] if r_lo else 0.0
    real_lo_oos  = np.nanmean(lo_cost[oos_mask])
    null_full, null_oos = [], []
    for _ in range(args.perms):
        sf, so = [], []
        for (holds, k, turn), is_oos in months_meta:
            if len(holds) < k:
                continue
            val = holds[RNG.permutation(len(holds))[:k]].mean() - turn * COST_RT
            sf.append(val)
            if is_oos:
                so.append(val)
        if len(sf) >= 6:
            null_full.append(np.mean(sf))
        if len(so) >= 6:
            null_oos.append(np.mean(so))
    null_full, null_oos = np.array(null_full), np.array(null_oos)
    p_full = (np.sum(null_full >= real_lo_full) + 1) / (len(null_full) + 1)
    p_oos  = (np.sum(null_oos  >= real_lo_oos ) + 1) / (len(null_oos) + 1)
    print(f"  LONG-only vs random basket — does momentum RANK add over random survivor longs?")
    print(f"    FULL: real {real_lo_full*100:+.2f}%/mo  random {null_full.mean()*100:+.2f}% "
          f"(p95 {np.percentile(null_full,95)*100:+.2f}%)  p={p_full:.3f}")
    print(f"    OOS : real {real_lo_oos*100:+.2f}%/mo  random {null_oos.mean()*100:+.2f}% "
          f"(p95 {np.percentile(null_oos,95)*100:+.2f}%)  p={p_oos:.3f}   ← the decisive one")
    print("  → high p (esp. OOS) = long-only 'edge' is beta+survivorship+equal-weight, NOT momentum.")

    print("\n" + "=" * 78)
    surv = "  ⚠ SURVIVORSHIP: universe = TODAY's F&O list → result is an UPPER bound."
    realb = (r_ls and r_ls["t"] > 2 and r_ls["mean"] > 0 and p < 0.05)
    if realb:
        print("  VERDICT: cross-sectional momentum L/S is significant NET of costs AND beats")
        print("  the random null. Decayed/crash-prone + survivorship-inflated — treat as an")
        print("  UPPER bound worth a small forward test, not free money.")
    else:
        print("  VERDICT: cross-sectional momentum does NOT clear the bar NET of costs / null")
        print(f"  (L/S NET t={r_ls['t'] if r_ls else 0:+.2f}, null p={p:.3f}). No edge here either.")
    print(surv)
    print("=" * 78)


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
