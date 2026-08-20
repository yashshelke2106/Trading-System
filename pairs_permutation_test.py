"""

*** CLOSED 2026-08-20 - H-019 REJECTED. Do not deploy, do not re-propose. ***

    The "one lead that survived" did not survive its own re-test. Under
    pairs_validate_v2.py (correlation-MATCHED null + monthly futures roll on
    both legs):
        Sharpe 0.49 (was 1.26 before roll costs), p = 0.190 vs matched null.
        One pair, TCS_INFY, carries it; without that pair Sharpe = 0.21.
        The validated 8-pair book needs ~Rs 21 lakh of futures margin
        (Rs 1.34 lakh/leg measured) against Rs 10 lakh available, so the
        diversification that produced the low drawdown is unfundable.
    The earlier Sharpe 1.26 / p=0.010 came from a WEAK null (random pairs) and
    no roll cost. Kept in the tree as the worked example of that mistake.

    NOTE: this harness uses the WEAK null (random pairs). Its p=0.010
    is superseded by pairs_validate_v2.py's matched null (p=0.190).

pairs_permutation_test.py — the falsification test for the pairs edge.

The committed pairs_program.py already does IS-cointegration-select / OOS-trade-blind
with a cost sweep and a both-halves stability gate. What it CANNOT tell you:
is the *cointegration selection* the source of any OOS edge, or would ANY pair of
correlated stocks traded with the same z-reversion rules do just as well?

This harness answers that with a PERMUTATION / PLACEBO test:

  1. REAL portfolio: pairs picked by cointegration on IS, traded blind on OOS.
  2. NULL portfolios: N random portfolios of the SAME SIZE, drawn from random
     (a,b) pairs in the universe — same hedge-beta construction, same z-entry/
     exit/stop machinery, same costs — but WITHOUT the cointegration filter.
  3. p-value = P(random-portfolio Sharpe >= real-portfolio Sharpe).

If p is small (<0.05), cointegration selection genuinely adds alpha.
If p is large, the "edge" is just mean-reversion on any correlated pair —
i.e. a property of the *trading rule*, not of the *selection*, and almost
certainly not survivable (no reason it would hold forward).

Plus: a longer, multi-regime window (default 3000d ~ 2018 -> 2026, INCLUDING the
2020 COVID crash), and an OOS first/second-half stability read.

Run:  .venv/Scripts/python.exe pairs_permutation_test.py --limit 120 --days 3000 --nperm 200
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from itertools import combinations

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_live_pipeline import fetch_daily, DEFAULT_UNIVERSE
from pairs_program import (
    select_pairs, pair_daily, _port_stats, half_life,
    IS_FRAC, ROLL, MIN_OOS_TRADES,
)


def _beta_on_is(la: pd.Series, lb: pd.Series):
    """Hedge beta from an IS OLS of log(a) on log(b) — same construction the
    cointegration path uses, so the ONLY thing the null removes is selection."""
    idx = la.dropna().index.intersection(lb.dropna().index)
    if len(idx) < 100:
        return None
    x, y = lb.loc[idx].values, la.loc[idx].values
    beta, _ = np.polyfit(x, y, 1)
    return float(beta) if beta > 0 else None


def _portfolio(pc_oos: pd.DataFrame, picks, cost: float):
    """picks = list of (a, b, beta). Returns (port_daily_series, all_trades)."""
    series, trades = [], []
    for a, b, beta in picks:
        d, tr = pair_daily(pc_oos, a, b, beta, cost)
        if len(d) and len(tr) >= MIN_OOS_TRADES:
            series.append(d.rename(f"{a}_{b}"))
            trades.extend(tr)
    if not series:
        return pd.Series(dtype=float), []
    port = pd.concat(series, axis=1).fillna(0.0).mean(axis=1)
    return port, trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3000)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--nperm", type=int, default=200)
    ap.add_argument("--cost", type=float, default=0.0010, help="per leg-change; RT=2x")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    try:
        from core.universe import FO_UNIVERSE
        syms = list(dict.fromkeys(FO_UNIVERSE))
    except Exception:
        syms = DEFAULT_UNIVERSE
    syms = syms[:args.limit] if args.limit else syms

    print(f"[PERM] fetching {len(syms)} names ({args.days}d) ...")
    C = {}
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is not None and len(df) > 320:
            df = df[~df.index.duplicated(keep="last")]
            C[s] = df["close"]
    pc = pd.DataFrame(C).sort_index().ffill().dropna(how="all").dropna(axis=1)
    if pc.shape[1] < 8:
        print("[PERM] FATAL: not enough names with full history."); return 1
    n = len(pc); cut = int(n * IS_FRAC)
    pc_is, pc_oos = pc.iloc[:cut], pc.iloc[cut:]
    logs_is = np.log(pc_is)
    print(f"[PERM] {pc.shape[1]} names x {n} days. "
          f"IS {pc.index[0].date()}..{pc.index[cut-1].date()}  "
          f"OOS {pc.index[cut].date()}..{pc.index[-1].date()}")

    # ---- REAL portfolio (cointegration-selected) ----
    pairs = select_pairs(pc_is)
    if not pairs:
        print("[PERM] no cointegrated pairs found."); return 0
    real_picks = [(a, b, beta) for (a, b, beta, hl) in pairs]
    k = len(real_picks)
    real_port, real_trades = _portfolio(pc_oos, real_picks, args.cost)
    rs = _port_stats(real_port)
    if not rs:
        print("[PERM] real portfolio has no tradeable OOS series."); return 0
    real_sharpe = rs["sharpe"]
    tr = np.array(real_trades)
    pf = (tr[tr > 0].sum() / -tr[tr < 0].sum()) if (tr < 0).any() else float("inf")
    half = len(real_port) // 2
    h1 = _port_stats(real_port.iloc[:half]); h2 = _port_stats(real_port.iloc[half:])
    print(f"\n[REAL] {k} cointegrated pairs, OOS @ {args.cost*100:.2f}%/leg-change:")
    print(f"   Sharpe {real_sharpe:.2f}  ann {rs['ann']:+.1f}%  maxDD {rs['mdd']:.1f}%  "
          f"PF {pf:.2f}  trades {len(tr)}")
    print(f"   OOS halves Sharpe: {h1.get('sharpe',0):.2f} / {h2.get('sharpe',0):.2f}")

    # ---- NULL distribution (random pairs, same size, same machinery) ----
    cols = list(pc.columns)
    all_combos = list(combinations(range(len(cols)), 2))
    print(f"\n[NULL] running {args.nperm} random-pair portfolios of {k} pairs each ...")
    null_sharpes = []
    attempts = 0
    while len(null_sharpes) < args.nperm and attempts < args.nperm * 40:
        attempts += 1
        idxs = rng.choice(len(all_combos), size=min(k * 3, len(all_combos)), replace=False)
        picks = []
        for ci in idxs:
            ia, ib = all_combos[ci]
            a, b = cols[ia], cols[ib]
            beta = _beta_on_is(logs_is[a], logs_is[b])
            if beta is not None:
                picks.append((a, b, beta))
            if len(picks) >= k:
                break
        if len(picks) < max(1, k // 2):
            continue
        port, _ = _portfolio(pc_oos, picks, args.cost)
        st = _port_stats(port)
        if st:
            null_sharpes.append(st["sharpe"])
    null = np.array(null_sharpes)
    if null.size == 0:
        print("[NULL] FATAL: no null portfolios produced."); return 1

    p_value = (1 + int((null >= real_sharpe).sum())) / (null.size + 1)
    print(f"   null Sharpe: mean {null.mean():+.2f}  sd {null.std():.2f}  "
          f"p5 {np.percentile(null,5):+.2f}  p95 {np.percentile(null,95):+.2f}  "
          f"max {null.max():+.2f}  (n={null.size})")

    print("\n" + "=" * 64)
    print("  PERMUTATION VERDICT")
    print("=" * 64)
    print(f"  real Sharpe {real_sharpe:+.2f}  vs  null mean {null.mean():+.2f}")
    print(f"  p-value (P[random >= real]) = {p_value:.3f}")
    halves_ok = h1.get("sharpe", 0) > 0 and h2.get("sharpe", 0) > 0
    if p_value < 0.05 and real_sharpe > 1.0 and halves_ok and rs["mdd"] > -25:
        print("  REAL: cointegration selection beats random pairs (p<0.05),")
        print("  Sharpe>1, both OOS halves positive, DD contained.")
        print("  -> worth a point-in-time, real-futures-cost forward test.")
    elif p_value < 0.05:
        print("  SELECTION ADDS SIGNAL (p<0.05) but fails a deploy gate")
        print(f"  (Sharpe {real_sharpe:.2f}, halves {h1.get('sharpe',0):.2f}/"
              f"{h2.get('sharpe',0):.2f}, DD {rs['mdd']:.1f}%). Real but not yet tradeable.")
    else:
        print("  MIRAGE: random correlated pairs do as well as the cointegrated ones")
        print(f"  (p={p_value:.2f}). The 'edge' is the z-reversion RULE, not the")
        print("  selection — and that rule has no reason to survive forward. Not deployable.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
