"""
pairs_validate_v2.py — the rigorous re-test of the pairs edge.

Upgrades over pairs_permutation_test.py, addressing the cross-review critique
(weak null, no roll costs, thin permutations, concentration risk):

  1. MATCHED NULL (the key fix). The null no longer draws random *junk* pairs.
     It draws CORRELATION-MATCHED pairs: same corr>0.60 prescreen the real
     selection uses, but WITHOUT the cointegration test. So the test now answers
     the only question that matters: does cointegration add value BEYOND mere
     correlation? (Beating uncorrelated darts was too easy and made p flattering.)

  2. ALTERNATIVE SELECTOR benchmark. Also trade the top-k *correlation-ranked*
     pairs (a real selector, not noise). If cointegration can't beat "just pick
     the most correlated pairs," it isn't earning its complexity.

  3. DISCRETE-FUTURES ROLL + 2026 STT costs. Stock futures expire monthly; a pair
     held for weeks crosses expiries and must roll BOTH legs. We charge a roll
     cost every ~21 trading days a position is held, on top of a realistic
     per-change cost (STT 0.05% sell-side from Apr-2026 + brokerage + impact).

  4. 500 permutations (cache makes it cheap) for a higher-resolution p-value.

  5. CONCENTRATION check: per-pair Sharpe contribution + portfolio Sharpe with the
     single best pair removed (is 1 pair carrying the book?).

REMAINING KNOWN CAVEAT (cannot fix without data): the universe is TODAY's F&O
list -> survivorship / look-ahead eligibility bias. True point-in-time needs
historical F&O membership (NSE add/drop circulars 2018-2026) we don't have.
Treat every positive number here as an OPTIMISTIC upper bound.

Run:  .venv/Scripts/python.exe pairs_validate_v2.py --limit 120 --days 3000 --nperm 500
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from itertools import combinations
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.bar_cache import cached_daily          # cache-first; no Dhan throttle
from pairs_program import (
    is_cointegrated, half_life, _port_stats,
    IS_FRAC, PRESCREEN_CORR, ROLL, Z_ENTRY, Z_EXIT, Z_STOP, MAX_PAIRS,
    HL_MIN, HL_MAX, MIN_OOS_TRADES,
)

# realistic Indian stock-futures frictions
COST_PER_CHANGE = 0.0015      # both legs, one position change (STT sell 0.05% + brokerage + impact)
ROLL_COST       = 0.0012      # both legs, per monthly roll while a position is held
ROLL_EVERY      = 21          # ~trading days between futures expiries


def _beta_is(la: pd.Series, lb: pd.Series) -> Optional[float]:
    idx = la.dropna().index.intersection(lb.dropna().index)
    if len(idx) < 100:
        return None
    x, y = lb.loc[idx].values, la.loc[idx].values
    beta, _ = np.polyfit(x, y, 1)
    return float(beta) if beta > 0 else None


def pair_daily_roll(pc_oos: pd.DataFrame, a: str, b: str, beta: float,
                    cost: float, roll_cost: float) -> Tuple[pd.Series, List[float]]:
    """Causal OOS daily P&L for one pair, WITH monthly roll costs charged on
    held positions. Mirrors pairs_program.pair_daily otherwise."""
    spread = (np.log(pc_oos[a]) - beta * np.log(pc_oos[b])).dropna()
    if len(spread) < ROLL + 5:
        return pd.Series(dtype=float), []
    z = (spread - spread.rolling(ROLL).mean()) / spread.rolling(ROLL).std()
    pnl = pd.Series(0.0, index=spread.index)
    trades: List[float] = []
    pos, entry_spread, held = 0, 0.0, 0
    for i in range(ROLL, len(spread) - 1):
        zi = z.iloc[i]
        if pos == 0:
            newpos = -1 if zi >= Z_ENTRY else (1 if zi <= -Z_ENTRY else 0)
        else:
            newpos = 0 if (abs(zi) <= Z_EXIT or abs(zi) >= Z_STOP) else pos
        roll = 0.0
        if pos != 0:
            held += 1
            if held % ROLL_EVERY == 0:          # crossed a futures expiry -> roll both legs
                roll = roll_cost
                pnl.iloc[i + 1] -= roll_cost
        if newpos != pos:
            pnl.iloc[i + 1] -= cost
            if pos == 0 and newpos != 0:
                entry_spread, held = spread.iloc[i], 0
            elif newpos == 0 and pos != 0:
                trades.append(pos * (spread.iloc[i] - entry_spread) - 2 * cost - roll)
                held = 0
        if newpos != 0:
            pnl.iloc[i + 1] += newpos * (spread.iloc[i + 1] - spread.iloc[i])
        pos = newpos
    return pnl, trades


def _portfolio(pc_oos, picks, cost, roll_cost):
    series, trades, per = [], [], {}
    for a, b, beta in picks:
        d, tr = pair_daily_roll(pc_oos, a, b, beta, cost, roll_cost)
        if len(d) and len(tr) >= MIN_OOS_TRADES:
            name = f"{a}_{b}"
            series.append(d.rename(name)); trades.extend(tr)
            st = _port_stats(d)
            per[name] = st.get("sharpe", 0.0) if st else 0.0
    if not series:
        return pd.Series(dtype=float), [], {}
    port = pd.concat(series, axis=1).fillna(0.0).mean(axis=1)
    return port, trades, per


def build_universe(pc_is: pd.DataFrame):
    """Return (coint_picks, corr_pool, n_tested). corr_pool = ALL corr>0.60 pairs
    with a valid IS beta (the matched-null draw space)."""
    cols = list(pc_is.columns)
    rets = pc_is.pct_change()
    cmat = rets.corr()
    logs = np.log(pc_is)
    coint, corr_pool, n_tested = [], [], 0
    for a, b in combinations(cols, 2):
        c = cmat.loc[a, b]
        if not np.isfinite(c) or c < PRESCREEN_CORR:
            continue
        beta = _beta_is(logs[a], logs[b])
        if beta is None:
            continue
        corr_pool.append((a, b, beta, float(c)))
        n_tested += 1
        ok, cbeta, hl = is_cointegrated(logs[a], logs[b])
        if ok and cbeta > 0:
            coint.append((a, b, cbeta, hl))
    coint.sort(key=lambda t: t[3])              # shortest half-life first
    return coint[:MAX_PAIRS], corr_pool, n_tested


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3000)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--nperm", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    from core.universe import FO_UNIVERSE
    syms = list(dict.fromkeys(FO_UNIVERSE))[:args.limit]
    print(f"[V2] loading {len(syms)} names ({args.days}d) via cache ...")
    C = {}
    for s in syms:
        df = cached_daily(s, args.days)
        if df is not None and len(df) > 320:
            C[s] = df["close"][~df["close"].index.duplicated(keep="last")]
    pc = pd.DataFrame(C).sort_index().ffill().dropna(how="all").dropna(axis=1)
    if pc.shape[1] < 8:
        print("[V2] FATAL: not enough names."); return 1
    n = len(pc); cut = int(n * IS_FRAC)
    pc_is, pc_oos = pc.iloc[:cut], pc.iloc[cut:]
    print(f"[V2] {pc.shape[1]} names x {n} days. "
          f"IS ..{pc.index[cut-1].date()}  OOS {pc.index[cut].date()}..{pc.index[-1].date()}")
    print(f"[V2] costs: {COST_PER_CHANGE*100:.2f}%/change + {ROLL_COST*100:.2f}%/roll "
          f"(every ~{ROLL_EVERY}d held)")

    coint, corr_pool, n_tested = build_universe(pc_is)
    k = len(coint)
    print(f"[V2] tested {n_tested} corr>{PRESCREEN_CORR} pairs -> {k} cointegrated kept "
          f"(corr pool for matched null: {len(corr_pool)})")
    if k < 3:
        print("[V2] too few cointegrated pairs for a stable verdict."); return 0

    # ---- REAL (cointegration-selected) ----
    real_picks = [(a, b, beta) for (a, b, beta, hl) in coint]
    port, trades, per = _portfolio(pc_oos, real_picks, COST_PER_CHANGE, ROLL_COST)
    rs = _port_stats(port)
    if not rs:
        print("[V2] real portfolio not tradeable after roll costs."); return 0
    tr = np.array(trades)
    pf = (tr[tr > 0].sum() / -tr[tr < 0].sum()) if (tr < 0).any() else float("inf")
    half = len(port) // 2
    h1, h2 = _port_stats(port.iloc[:half]), _port_stats(port.iloc[half:])
    print(f"\n[REAL cointegration] Sharpe {rs['sharpe']:.2f}  ann {rs['ann']:+.1f}%  "
          f"maxDD {rs['mdd']:.1f}%  PF {pf:.2f}  trades {len(tr)}  "
          f"halves {h1.get('sharpe',0):.2f}/{h2.get('sharpe',0):.2f}")

    # concentration: drop the single best-contributing pair
    if per:
        best = max(per, key=per.get)
        keep = [(a, b, bt) for (a, b, bt) in real_picks if f"{a}_{b}" != best]
        p2, _, _ = _portfolio(pc_oos, keep, COST_PER_CHANGE, ROLL_COST)
        s2 = _port_stats(p2)
        print(f"  concentration: best pair = {best} (Sharpe {per[best]:.2f}); "
              f"portfolio WITHOUT it -> Sharpe {s2.get('sharpe',0):.2f}")

    # ---- ALTERNATIVE SELECTOR: top-k by correlation ----
    corr_sorted = sorted(corr_pool, key=lambda t: t[3], reverse=True)[:k]
    cp, _, _ = _portfolio(pc_oos, [(a, b, bt) for (a, b, bt, c) in corr_sorted],
                          COST_PER_CHANGE, ROLL_COST)
    cs = _port_stats(cp)
    print(f"[ALT top-corr]      Sharpe {cs.get('sharpe',0):.2f}  "
          f"(does cointegration beat just-pick-most-correlated?)")

    # ---- MATCHED NULL: random k-pair draws from the corr>0.60 pool ----
    print(f"\n[NULL matched] {args.nperm} random {k}-pair portfolios from the "
          f"corr-matched pool ...")
    null = []
    for _ in range(args.nperm):
        idx = rng.choice(len(corr_pool), size=min(k, len(corr_pool)), replace=False)
        picks = [(corr_pool[j][0], corr_pool[j][1], corr_pool[j][2]) for j in idx]
        pp, _, _ = _portfolio(pc_oos, picks, COST_PER_CHANGE, ROLL_COST)
        st = _port_stats(pp)
        if st:
            null.append(st["sharpe"])
    null = np.array(null)
    p_val = (1 + int((null >= rs["sharpe"]).sum())) / (null.size + 1)
    print(f"   matched-null Sharpe: mean {null.mean():+.2f}  sd {null.std():.2f}  "
          f"p95 {np.percentile(null,95):+.2f}  max {null.max():+.2f}  (n={null.size})")

    print("\n" + "=" * 64)
    print("  RIGOROUS VERDICT (matched null + roll costs)")
    print("=" * 64)
    print(f"  real {rs['sharpe']:+.2f}  vs  corr-matched null mean {null.mean():+.2f}  "
          f"| top-corr selector {cs.get('sharpe',0):+.2f}")
    print(f"  p-value (cointegration beats corr-matched) = {p_val:.3f}")
    halves_ok = h1.get("sharpe", 0) > 0 and h2.get("sharpe", 0) > 0
    beats_alt = rs["sharpe"] > cs.get("sharpe", 0)
    if p_val < 0.05 and rs["sharpe"] > 1.0 and halves_ok and beats_alt and rs["mdd"] > -25:
        print("  SURVIVES: cointegration beats corr-matched darts AND a real")
        print("  correlation selector, Sharpe>1 after roll costs, both halves +.")
        print("  -> NOW earns a paper-forward-test (survivorship still a caveat).")
    elif p_val < 0.05 and beats_alt:
        print("  PARTIAL: cointegration adds signal but misses a deploy gate")
        print(f"  (Sharpe {rs['sharpe']:.2f}, halves {h1.get('sharpe',0):.2f}/"
              f"{h2.get('sharpe',0):.2f}, DD {rs['mdd']:.1f}%). Real, not yet tradeable.")
    else:
        print("  FAILS the rigorous bar: cointegration does NOT clearly beat the")
        print(f"  matched null (p={p_val:.2f}) / top-corr selector ({cs.get('sharpe',0):.2f}).")
        print("  The earlier p=0.01 was the weak-null artifact. Not deployable.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
