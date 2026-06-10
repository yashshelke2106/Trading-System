"""
pairs_program.py — a diversified, market-neutral statistical-arbitrage program.
The honest build of the one lead that survived the hunt (cointegrated pairs).

DISCIPLINE (what keeps this from being curve-fit):
  * Pairs are SELECTED by COINTEGRATION on the IN-SAMPLE half only — a structural
    property (the spread is mean-reverting), NOT by which pairs made money. Then
    those exact pairs are traded BLIND on the held-out OOS half. Selecting on the
    data you then trade is the classic look-ahead; we never do that.
  * Fixed textbook params (z>2 in, z<0.5 out, z>3.5 stop). No tuning.
  * Many pairs -> a PORTFOLIO. Diversification cuts drawdown (the point of
    market-neutral). We report portfolio Sharpe + max drawdown, not just per-trade.
  * Net of costs, with a sweep. India reality: you can't hold a short stock
    overnight, so BOTH legs are stock FUTURES -> higher cost + lot lumpiness.
    The sweep tests whether it survives that.

HONEST LIMITS (read before believing any positive result):
  * Survivorship: universe = today's F&O names (optimistic).
  * Multiple testing: we scan many pairs; the IS-select/OOS-trade split is the
    guard, but a positive OOS still needs a point-in-time, multi-regime re-test.
  * Beta/cointegration can BREAK out-of-sample (a merger, a sector shock) —
    that's the real risk of pairs, and no backtest fully captures it.

Run:  python pairs_program.py --full --limit 150 --days 1460
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

from backtest_live_pipeline import fetch_daily, DEFAULT_UNIVERSE
try:
    import config
    COST_RT = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06)) / 100.0
except Exception:
    COST_RT = 0.0006

try:
    from statsmodels.tsa.stattools import adfuller
    _HAVE_SM = True
except Exception:
    _HAVE_SM = False

# Fixed params
IS_FRAC      = 0.60
PRESCREEN_CORR = 0.60      # only test cointegration on return-correlated pairs (cuts compute)
ADF_P        = 0.05        # cointegration: ADF p-value on the IS residual spread
OU_T         = -3.0        # fallback stationarity: OU mean-reversion t-stat threshold
HL_MIN, HL_MAX = 3.0, 40.0 # acceptable half-life (days)
MAX_PAIRS    = 40          # cap portfolio breadth (best cointegration first)
Z_ENTRY, Z_EXIT, Z_STOP = 2.0, 0.5, 3.5
ROLL         = 30
MIN_OOS_TRADES = 2


def half_life(spread: pd.Series) -> float:
    s = spread.dropna()
    if len(s) < 40:
        return np.inf
    ds = s.diff().dropna()
    lag = s.shift(1).dropna()
    idx = ds.index.intersection(lag.index)
    x, y = lag.loc[idx].values, ds.loc[idx].values
    beta = np.polyfit(x, y, 1)[0]
    return float(-np.log(2) / beta) if beta < 0 else np.inf


def ou_tstat(spread: pd.Series) -> float:
    """t-stat of mean-reversion coefficient (negative = mean-reverting)."""
    s = spread.dropna()
    ds = s.diff().dropna()
    lag = s.shift(1).dropna()
    idx = ds.index.intersection(lag.index)
    x, y = lag.loc[idx].values, ds.loc[idx].values
    n = len(x)
    if n < 30:
        return 0.0
    b, a = np.polyfit(x, y, 1)
    resid = y - (b * x + a)
    se = np.sqrt(np.sum(resid**2) / (n - 2) / np.sum((x - x.mean())**2))
    return float(b / se) if se > 0 else 0.0


def is_cointegrated(la: pd.Series, lb: pd.Series) -> Tuple[bool, float, float]:
    """IS test. Returns (ok, hedge_beta, half_life). Uses ADF on residual if
    statsmodels present, else OU t-stat."""
    idx = la.dropna().index.intersection(lb.dropna().index)
    if len(idx) < 100:
        return False, 0.0, np.inf
    x, y = lb.loc[idx].values, la.loc[idx].values
    beta, alpha = np.polyfit(x, y, 1)
    spread = pd.Series(y - (beta * x + alpha), index=idx)
    hl = half_life(spread)
    if not (HL_MIN <= hl <= HL_MAX):
        return False, beta, hl
    if _HAVE_SM:
        try:
            p = adfuller(spread.values, maxlag=1, autolag=None)[1]
            return (p < ADF_P), beta, hl
        except Exception:
            pass
    return (ou_tstat(spread) < OU_T), beta, hl


def select_pairs(pc_is: pd.DataFrame) -> List[Tuple[str, str, float, float]]:
    cols = list(pc_is.columns)
    rets = pc_is.pct_change()
    logs = np.log(pc_is)
    out = []
    for a, b in combinations(cols, 2):
        r = rets[[a, b]].dropna()
        if len(r) < 100 or r[a].corr(r[b]) < PRESCREEN_CORR:
            continue
        ok, beta, hl = is_cointegrated(logs[a], logs[b])
        if ok and beta > 0:
            out.append((a, b, beta, hl))
    out.sort(key=lambda t: t[3])           # best (shortest) half-life first
    return out[:MAX_PAIRS]


def pair_daily(pc_oos: pd.DataFrame, a: str, b: str, beta: float,
               cost: float) -> Tuple[pd.Series, List[float]]:
    """Causal daily P&L series + list of per-trade returns for ONE pair on OOS."""
    spread = (np.log(pc_oos[a]) - beta * np.log(pc_oos[b])).dropna()
    if len(spread) < ROLL + 5:
        return pd.Series(dtype=float), []
    z = (spread - spread.rolling(ROLL).mean()) / spread.rolling(ROLL).std()
    pnl = pd.Series(0.0, index=spread.index)
    trades: List[float] = []
    pos, entry_spread = 0, 0.0
    for i in range(ROLL, len(spread) - 1):
        zi = z.iloc[i]
        if pos == 0:
            newpos = -1 if zi >= Z_ENTRY else (1 if zi <= -Z_ENTRY else 0)
        else:
            newpos = 0 if (abs(zi) <= Z_EXIT or abs(zi) >= Z_STOP) else pos
        if newpos != pos:
            pnl.iloc[i + 1] -= cost
            if pos == 0 and newpos != 0:          # opened
                entry_spread = spread.iloc[i]
            elif newpos == 0 and pos != 0:         # closed -> record trade
                trades.append(pos * (spread.iloc[i] - entry_spread) - 2 * cost)
        if newpos != 0:
            pnl.iloc[i + 1] += newpos * (spread.iloc[i + 1] - spread.iloc[i])
        pos = newpos
    return pnl, trades


def _port_stats(daily: pd.Series) -> Dict:
    d = daily.dropna()
    if len(d) < 30 or d.std() == 0:
        return {}
    ann = float(d.mean() * 252 * 100)
    sharpe = float(d.mean() / d.std() * np.sqrt(252))
    eq = (1 + d).cumprod()
    peak = eq.cummax()
    mdd = float(((eq - peak) / peak).min() * 100)
    return {"ann": ann, "sharpe": sharpe, "mdd": mdd,
            "pct_pos_days": float((d > 0).mean() * 100)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--limit", type=int, default=150)
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

    print(f"[SA] cointegration engine: {'statsmodels ADF' if _HAVE_SM else 'OU t-stat fallback'}")
    print(f"[SA] fetching {len(syms)} names ({args.days}d) ...")
    C = {}
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is not None and len(df) > 320:
            df = df[~df.index.duplicated(keep="last")]
            C[s] = df["close"]
    pc = pd.DataFrame(C).sort_index().ffill().dropna(how="all").dropna(axis=1)
    if pc.shape[1] < 8:
        print("[SA] FATAL: not enough names with full history."); return 1
    n = len(pc); cut = int(n * IS_FRAC)
    pc_is, pc_oos = pc.iloc[:cut], pc.iloc[cut:]
    print(f"[SA] {pc.shape[1]} names x {n} days. "
          f"IS {pc.index[0].date()}..{pc.index[cut-1].date()}  "
          f"OOS {pc.index[cut].date()}..{pc.index[-1].date()}")

    pairs = select_pairs(pc_is)
    print(f"[SA] selected {len(pairs)} cointegrated pairs IN-SAMPLE "
          f"(corr>{PRESCREEN_CORR}, half-life {HL_MIN}-{HL_MAX}d):")
    for a, b, beta, hl in pairs[:25]:
        print(f"   {a:12s} ~ {b:12s}  beta={beta:5.2f}  half-life={hl:4.1f}d")
    if not pairs:
        print("[SA] no cointegrated pairs found."); return 0

    print("\n" + "=" * 72)
    print("  OUT-OF-SAMPLE market-neutral portfolio (pairs picked on IS, traded blind)")
    print("=" * 72)
    print(f"  {'cost/leg-change':>15} {'trades':>6} {'WR%':>5} {'PF':>6} "
          f"{'exp%/tr':>8} | {'portAnn%':>8} {'Sharpe':>6} {'maxDD%':>7}")
    realistic = None
    for cst in (0.0006, 0.0010, 0.0015):    # per position change (both legs); RT = 2x
        per_pair_daily, all_trades = [], []
        for a, b, beta, hl in pairs:
            dseries, trades = pair_daily(pc_oos, a, b, beta, cst)
            if len(dseries) and len(trades) >= MIN_OOS_TRADES:
                per_pair_daily.append(dseries.rename(f"{a}_{b}"))
                all_trades.extend(trades)
        if not per_pair_daily:
            print(f"  {cst*100:>14.2f}%  no qualifying OOS trades"); continue
        port = pd.concat(per_pair_daily, axis=1).fillna(0.0).mean(axis=1)
        ps = _port_stats(port)
        tr = np.array(all_trades)
        w = tr[tr > 0]; l = tr[tr < 0]
        pf = w.sum() / -l.sum() if l.size else float("inf")
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        wr = 100 * len(w) / len(tr) if len(tr) else 0
        exp = tr.mean() * 100 if len(tr) else 0
        print(f"  {cst*100:>14.2f}%  {len(tr):>6} {wr:>5.0f} {pf_s:>6} {exp:>+7.3f}% | "
              f"{ps.get('ann',0):>+7.1f} {ps.get('sharpe',0):>6.2f} {ps.get('mdd',0):>7.1f}")
        if abs(cst - 0.0010) < 1e-9:
            realistic = (ps, pf, exp, len(tr), port)
    print("=" * 72)

    # held-out-within-OOS stability + verdict at realistic 0.10%/change (0.20% RT)
    if realistic:
        ps, pf, exp, ntr, port = realistic
        half = len(port) // 2
        h1, h2 = _port_stats(port.iloc[:half]), _port_stats(port.iloc[half:])
        print(f"\n  @ realistic 0.10%/change (0.20% round-trip), OOS portfolio:")
        print(f"     ann {ps['ann']:+.1f}%  Sharpe {ps['sharpe']:.2f}  maxDD {ps['mdd']:.1f}%  "
              f"+days {ps['pct_pos_days']:.0f}%  trades {ntr}")
        print(f"     OOS first-half Sharpe {h1.get('sharpe',0):.2f} | "
              f"second-half Sharpe {h2.get('sharpe',0):.2f}")
        print("\n  VERDICT")
        good = (ps["sharpe"] > 1.0 and exp > 0 and pf > 1.2
                and h1.get("sharpe", 0) > 0 and h2.get("sharpe", 0) > 0
                and ps["mdd"] > -25)
        if good:
            print("  REAL CANDIDATE: positive, market-neutral, Sharpe>1, both OOS halves")
            print("  positive, drawdown contained. THIS IS WORTH A SERIOUS FORWARD TEST.")
            print("  Next, before any capital: point-in-time universe (no survivorship),")
            print("  a crash regime (2020), REAL stock-futures costs + lot-size sizing,")
            print("  and watch for pairs whose cointegration BREAKS live.")
        else:
            print("  Does NOT clear the bar (need Sharpe>1, PF>1.2, both halves +, DD>-25%).")
            print(f"  Got Sharpe {ps['sharpe']:.2f}, PF {pf if isinstance(pf,float) else pf:.2f}, "
                  f"exp {exp:+.3f}%, halves {h1.get('sharpe',0):.2f}/{h2.get('sharpe',0):.2f}.")
            print("  Consistent with a real-but-decayed edge that costs erode. Not deployable.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
