"""
Volatility Risk Premium (VRP) research — the one edge with a real reason to exist.

THE HYPOTHESIS (with a named counterparty, unlike price patterns):
  Option BUYERS overpay for protection. So IMPLIED volatility (what they pay,
  = India VIX) systematically exceeds the REALIZED volatility that follows.
  A seller of that protection earns the difference. The counterparty loses
  because they're buying insurance they usually don't need — a structural,
  decades-durable reason, not a chart pattern.

THE MEASUREMENT (canonical, academic):
  VRP_t = ImpliedVol_t  -  RealizedVol_[t, t+h]
        = IndiaVIX_t     -  annualised stdev of NIFTY daily returns over next h days
  If VRP is persistently POSITIVE, selling vol earns the premium.

THE CATCH (why this is dangerous, and why the harness foregrounds the TAIL):
  VRP is positive on AVERAGE but pays back VIOLENTLY in crashes (realized
  spikes far above implied). The mean looks great; the tail kills accounts.
  "Picking up pennies in front of a steamroller." So this harness reports the
  mean AND the worst single period AND the max drawdown of cumulative VRP —
  you must see both to judge it honestly. A positive mean is necessary but NOT
  sufficient: the tail has to be survivable with defined-risk sizing.

HONEST LIMITS:
  - This measures the VRP SPREAD (VIX − realized), the academic edge metric.
    Real option selling adds gamma path-dependency, bid/ask, and fatter tails
    than the spread implies — treat a positive result as "worth a defined-risk
    forward test", not "free money".
  - Needs India VIX history (Dhan IDX_I id 21) + NIFTY. ~7yr ideal.

Run (your machine):
    python research_vrp.py                 # 30-day horizon, monthly
    python research_vrp.py --horizon 7     # weekly vol selling
    python research_vrp.py --days 2600     # ~7 years of history
"""

from __future__ import annotations

import argparse
import sys
try:                                  # Windows cp1252 chokes on ≈/⚠/→ in output
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import warnings
warnings.filterwarnings("ignore")
import logging
logging.getLogger("core.api_dhan").setLevel(logging.CRITICAL)

import numpy as np
import pandas as pd

DAYS = 2600
HORIZON = 30          # trading days of realized vol to compare against implied
REBAL = 21            # step between observations (overlap is handled in stats)
OOS_FRACTION = 0.30
ANN = np.sqrt(252)    # annualisation factor for daily-return stdev


def _series(symbol: str, days: int):
    from core.api_dhan import dhan_daily
    d = dhan_daily(symbol, days_back=days)
    if d is None or d.empty:
        return None
    d = d.copy(); d.columns = [c.lower() for c in d.columns]
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    s = d.set_index("date")["close"]
    return s[~s.index.duplicated(keep="last")].sort_index()


def realized_vol(nifty: pd.Series, start_i: int, h: int) -> float:
    """Annualised realized vol (%) of NIFTY over [start_i, start_i+h]."""
    window = nifty.iloc[start_i:start_i + h + 1]
    if len(window) < max(5, h // 2):
        return np.nan
    rets = np.log(window / window.shift(1)).dropna()
    return float(rets.std() * ANN * 100)


def newey_west_se(x: np.ndarray, lag: int) -> float:
    """HAC (Newey-West) standard error of the MEAN, correcting for the
    autocorrelation that OVERLAPPING windows inject. With step REBAL < window
    HORIZON, consecutive observations share data and are not independent —
    the naive SE (and the Sharpe built on it) overstate significance. The
    Bartlett-kernel HAC estimator fixes that. lag=0 → plain IID SE."""
    n = len(x)
    if n < 2:
        return float("nan")
    xc = x - x.mean()
    var = float(np.dot(xc, xc) / n)                      # gamma_0
    for k in range(1, min(lag, n - 1) + 1):
        gamma_k = float(np.dot(xc[k:], xc[:-k]) / n)
        var += 2.0 * (1.0 - k / (lag + 1)) * gamma_k     # Bartlett weight
    var = max(var, 0.0)
    return float(np.sqrt(var / n))


def summarize(vrps: np.ndarray, label: str, lag: int = 0) -> dict:
    if len(vrps) == 0:
        return {}
    mean = vrps.mean()
    pos = (vrps > 0).mean() * 100
    sd = vrps.std()
    sharpe = (mean / sd * np.sqrt(252 / HORIZON)) if sd > 0 else 0.0   # annualised-ish
    # Overlap-honest significance: HAC SE of the mean → t-stat you can trust.
    nw_se = newey_west_se(vrps, lag)
    t_stat = (mean / nw_se) if (nw_se and nw_se > 0) else 0.0
    worst = vrps.min()
    # cumulative-VRP "equity" drawdown — the steamroller view
    eq = np.cumsum(vrps); peak = np.maximum.accumulate(eq)
    mdd = float((eq - peak).min())
    # tail stats
    p5 = np.percentile(vrps, 5)
    kurt = float(pd.Series(vrps).kurt())   # excess kurtosis; >0 = fat tails
    return {"n": len(vrps), "mean": mean, "pos": pos, "sharpe": sharpe,
            "nw_se": nw_se, "t_stat": t_stat,
            "worst": worst, "p5": p5, "mdd": mdd, "kurt": kurt}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--no-overlap", action="store_true",
                    help="step = horizon so windows don't overlap (clean IID stats, fewer obs)")
    args = ap.parse_args()
    globals()["HORIZON"] = args.horizon
    # Overlap accounting: step REBAL, window HORIZON → ~HORIZON/REBAL obs share
    # data. --no-overlap steps a full horizon (independent). overlap_lag drives
    # the Newey-West correction.
    rebal = args.horizon if args.no_overlap else REBAL
    overlap_lag = 0 if rebal >= args.horizon else max(1, int(np.ceil(args.horizon / rebal)) - 1)

    print(f"[VRP] fetching India VIX + NIFTY ({args.days}d) from Dhan ...")
    vix = _series("INDIAVIX", args.days)
    nifty = _series("NIFTY", args.days)
    if vix is None or nifty is None:
        print("[VRP] FAIL — need INDIAVIX (id 21) and NIFTY from Dhan. "
              "Check security IDs / IDX_I segment.")
        return
    # align on common dates
    idx = vix.index.intersection(nifty.index)
    vix = vix.reindex(idx).dropna(); nifty = nifty.reindex(idx).dropna()
    idx = vix.index.intersection(nifty.index)
    vix = vix.reindex(idx); nifty = nifty.reindex(idx)
    print(f"[VRP] aligned {len(idx)} trading days  {idx[0].date()} -> {idx[-1].date()}\n")
    if len(idx) < args.horizon + 60:
        print("[VRP] not enough history. Try --days 2600."); return

    rows = []
    for i in range(0, len(idx) - args.horizon, rebal):
        implied = float(vix.iloc[i])                 # India VIX = annualised implied %
        rv = realized_vol(nifty, i, args.horizon)    # annualised realized %
        if np.isnan(rv) or implied <= 0:
            continue
        rows.append((idx[i], implied - rv, implied, rv))

    if not rows:
        print("[VRP] no observations."); return
    dates = [r[0] for r in rows]
    vrp = np.array([r[1] for r in rows])

    split = dates[int(len(dates) * (1 - OOS_FRACTION))]
    is_v = np.array([r[1] for r in rows if r[0] < split])
    oos_v = np.array([r[1] for r in rows if r[0] >= split])

    full = summarize(vrp, "full", overlap_lag)
    si, so = summarize(is_v, "IS", overlap_lag), summarize(oos_v, "OOS", overlap_lag)

    print("=" * 70)
    print("  VOLATILITY RISK PREMIUM — India VIX (implied) minus NIFTY realized")
    print(f"  horizon {args.horizon}d   n={full['n']}   OOS split {str(split)[:10]}")
    print("=" * 70)
    print(f"  mean VRP        {full['mean']:+.2f} vol-pts   (implied richer than realized by this)")
    print(f"  positive %      {full['pos']:.0f}%   of periods implied > realized")
    print(f"  ~Sharpe         {full['sharpe']:.2f}   (overlap-inflated — trust the t-stat)")
    print(f"  t-stat (HAC)    {full['t_stat']:+.2f}   overlap-lag={overlap_lag}   (|t|>2 ≈ real)")
    print(f"  IS  mean        {si['mean']:+.2f}   t={si['t_stat']:+.2f}   pos {si['pos']:.0f}%   (n={si['n']})")
    print(f"  OOS mean        {so['mean']:+.2f}   t={so['t_stat']:+.2f}   pos {so['pos']:.0f}%   (n={so['n']})")
    print("-" * 70)
    print("  THE TAIL (this is what kills short-vol accounts):")
    print(f"  worst period    {full['worst']:+.2f} vol-pts   (realized blew past implied = a crash)")
    print(f"  5th percentile  {full['p5']:+.2f}")
    print(f"  max drawdown    {full['mdd']:+.2f} cumulative vol-pts")
    print(f"  excess kurtosis {full['kurt']:+.1f}   (>0 = fat tails; short-vol P&L is NON-normal)")
    print("=" * 70)

    # verdict
    edge = (si["mean"] > 0 and so["mean"] > 0 and full["mean"] > 1.0)
    oos_sig = abs(so.get("t_stat", 0.0)) >= 2.0
    tail_ratio = abs(full["worst"]) / full["mean"] if full["mean"] != 0 else 1e9
    print()
    if not oos_sig:
        print(f"  ⚠ OOS t-stat {so['t_stat']:+.2f}: after the overlap correction the OOS")
        print("    premium is NOT statistically distinguishable from zero. A positive")
        print("    OOS mean here is within noise — do not bank on it.\n")
    if edge and tail_ratio < 8:
        print("  VERDICT: VRP is positive in BOTH halves and the tail looks survivable")
        print("  with DEFINED-RISK sizing. Worth a small forward paper test (short")
        print("  defined-risk strangles/spreads, never naked). NOT free money — the")
        print(f"  worst period ({full['worst']:+.1f}) is ~{tail_ratio:.0f}x the mean.")
    elif edge:
        print(f"  VERDICT: VRP positive both halves BUT the tail is brutal — worst")
        print(f"  period is ~{tail_ratio:.0f}x the mean premium. The edge is real on")
        print("  average; surviving the crash is the whole game. Defined-risk + tiny")
        print("  size only, and accept that one bad event can erase a year.")
    else:
        print("  VERDICT: VRP did not hold positive in both halves on this data /")
        print("  horizon. Either the window was crash-heavy or the horizon is off.")
        print("  Try --horizon 7 and --days 2600 before concluding.")
    print("\n  NOTE: this is the VRP SPREAD (the academic edge metric). Real option")
    print("  selling adds gamma path-risk, spreads, and fatter tails than the spread")
    print("  shows. A positive result = 'worth a defined-risk forward test', not 'go'.")


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
