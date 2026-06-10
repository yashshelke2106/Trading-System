"""
edge_overnight_conditioned.py — Hypothesis A: harvest the KNOWN-real overnight
drift edge by CONDITIONING, so it survives turnover cost.

The prior hunt found overnight drift is real GROSS (+0.061%/day equal-weight) but
dies NET because trading every name every night pays cost every night. This tests
whether a CROSS-SECTIONAL condition concentrates the overnight premium into a
small decile of name-nights with gross return high enough to beat cost.

Mechanism (name the counterparty): short-term REVERSAL of intraday overreaction
unwinds overnight — intraday sellers who overshoot into the close get bought back
at the open by overnight risk-bearers. So the biggest intraday LOSERS should bounce
overnight. That is a real, documented close-to-open phenomenon, not folklore.

THE TRAP (per cross-review): "ex-post thinning" — almost ANY rule that trades fewer
names can look better net by luck. So every condition is judged against a RANDOM-
NAME THINNING NULL: each day pick the SAME number of random names; if the condition
can't beat random selection of equal count, the 'edge' is just trading less, not
skill. p = P(random-thinning Sharpe >= conditioned Sharpe).

A-priori, monotone, single-variable conditions (no curve-fitting, no combining):
  REV   : bottom-decile by today's intraday return (pc/po-1)   -> overnight bounce
  MOM   : top-decile by today's intraday return                 -> overnight momentum
  LOWVOL: bottom-decile by 20d realized vol                     -> low-vol overnight premium
  DOWN  : bottom-decile by prior-day close-to-close return      -> reversal variant

Costs swept (per name-night round-trip, futures: STT sell + brokerage + impact).

Run:  .venv/Scripts/python.exe edge_overnight_conditioned.py --limit 150 --days 3000 --nperm 500
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.bar_cache import cached_daily

DECILE = 0.10
IS_FRAC = 0.60
TRADING_DAYS = 252


def _ann_sharpe(s: pd.Series):
    s = s.dropna()
    if len(s) < 20 or s.std() == 0:
        return 0.0, 0.0
    return float(s.mean() * TRADING_DAYS * 100), float(s.mean() / s.std() * np.sqrt(TRADING_DAYS))


def _maxdd(s: pd.Series) -> float:
    eq = (1 + s.fillna(0)).cumprod(); peak = eq.cummax()
    return float(((eq - peak) / peak).min() * 100)


def load_panel(syms, days):
    O, Cl = {}, {}
    for s in syms:
        df = cached_daily(s, days)
        if df is not None and len(df) > 400:
            df = df[~df.index.duplicated(keep="last")]
            O[s], Cl[s] = df["open"], df["close"]
    po = pd.DataFrame(O).sort_index()
    pc = pd.DataFrame(Cl).sort_index()
    idx = po.index.intersection(pc.index)
    return po.loc[idx], pc.loc[idx]


def conditioned_daily(g: pd.DataFrame, rank: pd.DataFrame, bottom: bool, cost: float):
    """g = overnight gross returns (name x day). rank = predictor known at close.
    Each day select the DECILE (bottom or top) of rank; portfolio = mean net over
    selected names. Returns (daily_net_series, per_day_count)."""
    daily, counts = [], []
    idx = g.index
    for t in idx:
        gr, rk = g.loc[t], rank.loc[t]
        valid = gr.notna() & rk.notna()
        names = rk[valid].index
        if len(names) < 20:
            daily.append(np.nan); counts.append(0); continue
        k = max(1, int(len(names) * DECILE))
        order = rk[names].sort_values(ascending=bottom)   # bottom=True -> smallest first
        pick = order.index[:k]
        daily.append(float(gr[pick].mean() - cost)); counts.append(k)
    return pd.Series(daily, index=idx), pd.Series(counts, index=idx)


def thinning_null(g: pd.DataFrame, counts: pd.Series, cost: float, nperm: int, rng):
    """Each day pick `counts[t]` RANDOM valid names; portfolio mean net. Repeat
    nperm times -> distribution of Sharpe. Holds per-day count (and which days are
    active) fixed, randomizing only WHICH names -> isolates selection skill."""
    idx = g.index
    valid_names = {t: g.loc[t].dropna().index.values for t in idx}
    gv = {t: g.loc[t].dropna() for t in idx}
    sharpes = []
    for _ in range(nperm):
        daily = []
        for t in idx:
            k = counts[t]
            names = valid_names[t]
            if k <= 0 or len(names) < k:
                daily.append(np.nan); continue
            pick = rng.choice(names, size=k, replace=False)
            daily.append(float(gv[t][pick].mean() - cost))
        _, sh = _ann_sharpe(pd.Series(daily, index=idx))
        sharpes.append(sh)
    return np.array(sharpes)


def evaluate(name, g, rank, bottom, cost, nperm, rng):
    cond, counts = conditioned_daily(g, rank, bottom, cost)
    a, sh = _ann_sharpe(cond)
    h = len(cond.dropna()) // 2
    cd = cond.dropna()
    _, sh1 = _ann_sharpe(cd.iloc[:h]); _, sh2 = _ann_sharpe(cd.iloc[h:])
    dd = _maxdd(cond)
    null = thinning_null(g, counts, cost, nperm, rng)
    p = (1 + int((null >= sh).sum())) / (null.size + 1)
    ntr = int(counts.sum())
    print(f"  {name:7s} netAnn {a:+6.1f}%  Sharpe {sh:+.2f}  halves {sh1:+.2f}/{sh2:+.2f}  "
          f"DD {dd:5.1f}%  | thinNull mean {null.mean():+.2f} p95 {np.percentile(null,95):+.2f}  "
          f"p={p:.3f}  trades~{ntr}")
    passes = (sh > 0.7 and a > 0 and p < 0.05 and sh1 > 0 and sh2 > 0)
    return passes, (name, sh, p, a)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3000)
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--nperm", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--neutral", action="store_true",
                    help="market-neutral diagnostic: remove per-night cross-sectional mean")
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    from core.universe import FO_UNIVERSE
    syms = list(dict.fromkeys(FO_UNIVERSE))[:args.limit]
    print(f"[ON] loading {len(syms)} names ({args.days}d) via cache ...")
    po, pc = load_panel(syms, args.days)
    print(f"[ON] {pc.shape[1]} names x {pc.shape[0]} days  "
          f"{pc.index[0].date()} -> {pc.index[-1].date()}")

    # overnight gross at decision day t: hold close[t] -> open[t+1]
    g = (po.shift(-1) / pc - 1)
    # drop corporate-action gaps (splits/bonus/ex-date) — unadjusted >15% jumps are
    # not tradeable returns; they were the likely fuel for the extreme-mover deciles.
    g = g.where(g.abs() <= 0.15)
    if args.neutral:
        # strip market beta: subtract each night's cross-sectional mean overnight
        # return (cheap diagnostic, not a full beta-neutralization).
        g = g.sub(g.mean(axis=1), axis=0)
        print("[ON] MARKET-NEUTRAL mode: per-night cross-sectional mean removed")
    # predictors known at close[t] (no look-ahead)
    intraday = pc / po - 1                       # today's open->close (known at close)
    rvol = pc.pct_change().rolling(20).std()
    prior_cc = pc.pct_change()                   # yesterday->today close (known at close)

    n = len(g); cut = int(n * IS_FRAC)
    oos = slice(cut, n)
    g_oos = g.iloc[oos]
    conds = {
        "REV":    (intraday.iloc[oos], True),    # bottom intraday (biggest losers) -> bounce
        "MOM":    (intraday.iloc[oos], False),   # top intraday -> momentum
        "LOWVOL": (rvol.iloc[oos], True),        # lowest realized vol
        "DOWN":   (prior_cc.iloc[oos], True),    # biggest prior-day losers
    }

    # baseline: naive trade-ALL-names overnight, net of cost (the thing that died)
    print(f"\n[OOS {g.index[cut].date()}..{g.index[-1].date()}]  decile={DECILE:.0%}, "
          f"thinning-null n={args.nperm}")
    for cost in (0.0005, 0.0007, 0.0010):
        allnet = (g_oos - cost).mean(axis=1)
        a_all, sh_all = _ann_sharpe(allnet)
        print(f"\n--- cost {cost*100:.2f}%/name-night ---  "
              f"[naive ALL-names: netAnn {a_all:+.1f}% Sharpe {sh_all:+.2f}]")
        survivors = []
        for nm, (rk, bot) in conds.items():
            ok, info = evaluate(nm, g_oos, rk, bot, cost, args.nperm, rng)
            if ok:
                survivors.append(info)
        if survivors:
            print(f"  >> SURVIVES at {cost*100:.2f}%: " +
                  ", ".join(f"{s[0]}(Sh{s[1]:.2f},p{s[2]:.3f})" for s in survivors))
        else:
            print(f"  >> none beat the thinning null + net bar at {cost*100:.2f}%")

    print("\n" + "=" * 64)
    print("  Read: a condition is real ONLY if it beats its RANDOM-THINNING null")
    print("  (p<0.05) AND nets positive after cost AND holds both OOS halves.")
    print("  Survivorship caveat (today's F&O list) still applies as upper bound.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
