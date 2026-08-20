"""
Iron-condor backtest — the gate on the volatility risk premium.

REGISTRY: H-014 CONDITIONAL-PASS (defined-risk short-vol sleeve) and H-015 OPEN
(is 14d the right horizon, not the assumed 30d). "The only real edge" was a
claim this file made about itself while no hypothesis had closed as a PASS;
H-014 is conditional and H-015 is still open, so treat what follows as a
candidate under test rather than a validated result. Related: H-016 REJECTED
(no state where realized exceeds implied).

research_vrp.py proved the volatility risk premium is real and holds OOS
(implied > realized 77% of the time) — BUT with a catastrophic -57 vol-pt tail
and a thin recent premium (OOS +0.55). The open question: once you CAP that
tail with protective wings (defined risk) and pay real costs, is there still
net money? This harness answers it.

WHAT IT DOES, each month:
  1. Sell a strangle on NIFTY ~1 SD OTM (short call @ S+kσ, short put @ S-kσ),
     where σ = S · IV · √T and IV = India VIX. ~16-delta short legs.
  2. BUY wings further out (long call @ Kc+W, long put @ Kp-W) → IRON CONDOR.
     The wings CAP the loss at (W − net_credit). COVID can't blow the account.
  3. Price all four legs with Black-Scholes (European; NIFTY options are EU).
  4. Hold to expiry (--horizon days). Settle against where NIFTY ACTUALLY went.
  5. Subtract realistic round-trip costs (slippage + fees as % of premium).
  6. P&L expressed as RETURN ON CAPITAL-AT-RISK (net P&L / max loss), so it's
     comparable across vol regimes. Aggregate IS vs OOS.

THE VERDICT requires: positive mean return-on-risk in BOTH halves AND a worst
trade that is genuinely capped (it will be, by construction) AND OOS net > 0
after costs. If OOS goes negative after costs, the real edge is NOT harvestable
at retail scale — a true, money-saving answer.

HONEST MODEL CAVEATS:
  - Uses India VIX as a single IV for both strikes. Real NIFTY options have a
    PUT SKEW (puts richer) — that actually *helps* a put seller, so this is if
    anything conservative on the put side, optimistic on ignoring skew costs.
  - BS assumes you hold to expiry with no gamma management. Real condors are
    often managed early; results are a floor, not a ceiling.
  - Costs are a % proxy, not a live order book. Tune --cost to your broker.
  A positive OOS result here = "worth a tiny defined-risk forward test",
  still not "guaranteed money".

Run:  python research_iron_condor.py --days 2600 --horizon 30
      python research_iron_condor.py --delta 1.0 --wing 1.0 --cost 0.05
"""

from __future__ import annotations

import argparse
import math
import sys
try:                                  # Windows cp1252 chokes on σ/≈/→ in output
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
HORIZON = 30
REBAL = 21
OOS_FRACTION = 0.30
RISK_FREE = 0.065        # ~India 30-day risk-free
DELTA_MULT = 1.0         # short strikes at S ± DELTA_MULT·σ  (~1 SD ≈ 16-delta)
WING_MULT = 1.0          # wings WING_MULT·σ beyond the short strikes
COST_FRAC = 0.05         # round-trip slippage+fees as fraction of gross premium


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S: float, K: float, T: float, sigma: float, call: bool, r: float = RISK_FREE) -> float:
    """Black-Scholes European price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(0.0, (S - K) if call else (K - S))
        return intrinsic
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


# yfinance tickers for the two series this harness needs. The Dhan Data API
# subscription has expired (all /v2/charts/* return 401), so without this
# fallback the whole VRP gate is unrunnable. yfinance carries India VIX back to
# 2008 — DEEPER history than the Dhan path had, which is a strict improvement
# for a study whose entire risk lives in the crash tail (2008, 2020 both in).
_YF_FALLBACK = {"INDIAVIX": "^INDIAVIX", "NIFTY": "^NSEI"}


def _series(symbol: str, days: int):
    # Primary: Dhan.
    try:
        from core.api_dhan import dhan_daily
        d = dhan_daily(symbol, days_back=days)
        if d is not None and not d.empty:
            d = d.copy(); d.columns = [c.lower() for c in d.columns]
            d["date"] = pd.to_datetime(d["date"]).dt.normalize()
            s = d.set_index("date")["close"]
            return s[~s.index.duplicated(keep="last")].sort_index()
    except Exception:
        pass

    # Fallback: yfinance.
    tk = _YF_FALLBACK.get(symbol.upper())
    if not tk:
        return None
    try:
        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context
        import yfinance as yf
        raw = yf.Ticker(tk).history(period="max", interval="1d")
        if raw is None or raw.empty:
            return None
        s = raw["Close"].copy()
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        return s.tail(days) if days else s
    except Exception:
        return None


def newey_west_se(x: np.ndarray, lag: int) -> float:
    """HAC (Newey-West) standard error of the MEAN, correcting for the
    autocorrelation that OVERLAPPING windows inject (step REBAL < window
    HORIZON → non-independent trades). The naive SE / Sharpe overstate
    significance; the Bartlett-kernel HAC estimator fixes it. lag=0 → IID."""
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


def summarize(rets: np.ndarray, lag: int = 0) -> dict:
    if len(rets) == 0:
        return {}
    mean = rets.mean()
    sd = rets.std()
    sharpe = (mean / sd * np.sqrt(252 / HORIZON)) if sd > 0 else 0.0
    nw_se = newey_west_se(rets, lag)
    t_stat = (mean / nw_se) if (nw_se and nw_se > 0) else 0.0
    eq = np.cumprod(1 + rets); peak = np.maximum.accumulate(eq)
    mdd = float(((eq - peak) / peak).min() * 100)
    return {"n": len(rets), "mean_pct": mean * 100, "win": (rets > 0).mean() * 100,
            "sharpe": sharpe, "t_stat": t_stat, "nw_se": nw_se,
            "worst_pct": rets.min() * 100, "mdd_pct": mdd}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--delta", type=float, default=DELTA_MULT, help="short strikes at S ± this·σ")
    ap.add_argument("--wing", type=float, default=WING_MULT, help="wing width in units of σ")
    ap.add_argument("--cost", type=float, default=COST_FRAC, help="round-trip cost as frac of gross premium")
    ap.add_argument("--no-overlap", action="store_true",
                    help="step = horizon so trades don't overlap (clean IID stats, fewer obs)")
    args = ap.parse_args()
    globals()["HORIZON"] = args.horizon
    # Overlap accounting: step REBAL, hold HORIZON → ~HORIZON/REBAL trades share
    # a holding window and are not independent. --no-overlap steps a full
    # horizon. overlap_lag drives the Newey-West significance correction.
    rebal = args.horizon if args.no_overlap else REBAL
    overlap_lag = 0 if rebal >= args.horizon else max(1, int(np.ceil(args.horizon / rebal)) - 1)

    print(f"[IC] fetching India VIX + NIFTY ({args.days}d) ...")
    vix = _series("INDIAVIX", args.days); nifty = _series("NIFTY", args.days)
    if vix is None or nifty is None:
        print("[IC] FAIL — need INDIAVIX (id 21) + NIFTY from Dhan."); return
    idx = vix.index.intersection(nifty.index)
    vix = vix.reindex(idx).dropna(); nifty = nifty.reindex(idx).dropna()
    idx = vix.index.intersection(nifty.index)
    vix, nifty = vix.reindex(idx), nifty.reindex(idx)
    print(f"[IC] aligned {len(idx)} days  {idx[0].date()} -> {idx[-1].date()}")
    print(f"[IC] short@{args.delta}σ  wings@{args.wing}σ  cost={args.cost:.0%} of premium\n")
    if len(idx) < args.horizon + 60:
        print("[IC] not enough history. --days 2600."); return

    T = args.horizon / 252.0
    rows = []
    for i in range(0, len(idx) - args.horizon, rebal):
        S = float(nifty.iloc[i]); iv = float(vix.iloc[i]) / 100.0
        if S <= 0 or iv <= 0:
            continue
        sigma_move = S * iv * math.sqrt(T)          # 1 SD move in index points
        Kc = S + args.delta * sigma_move            # short call
        Kp = S - args.delta * sigma_move            # short put
        W = max(args.wing * sigma_move, 1.0)        # wing width
        # premiums (per index point; lot multiplier cancels in return-on-risk)
        short_call = bs(S, Kc, T, iv, call=True)
        short_put = bs(S, Kp, T, iv, call=False)
        long_call = bs(S, Kc + W, T, iv, call=True)
        long_put = bs(S, Kp - W, T, iv, call=False)
        gross_prem = short_call + short_put + long_call + long_put
        net_credit = (short_call + short_put) - (long_call + long_put)
        if net_credit <= 0:
            continue
        max_loss = W - net_credit                   # capital at risk (capped!)
        if max_loss <= 0:
            continue
        # settle at expiry against the ACTUAL move
        S_T = float(nifty.iloc[i + args.horizon])
        call_spread_loss = min(max(S_T - Kc, 0.0), W)   # short call spread
        put_spread_loss = min(max(Kp - S_T, 0.0), W)    # short put spread
        cost = args.cost * gross_prem
        net_pnl = net_credit - call_spread_loss - put_spread_loss - cost
        ret_on_risk = net_pnl / max_loss            # return on capital-at-risk
        rows.append((idx[i], ret_on_risk, net_pnl, max_loss))

    if not rows:
        print("[IC] no tradeable condors (credit<=0 every month?). Try --wing 1.5."); return
    dates = [r[0] for r in rows]
    rets = np.array([r[1] for r in rows])
    split = dates[int(len(dates) * (1 - OOS_FRACTION))]
    is_r = np.array([r[1] for r in rows if r[0] < split])
    oos_r = np.array([r[1] for r in rows if r[0] >= split])
    full = summarize(rets, overlap_lag)
    si, so = summarize(is_r, overlap_lag), summarize(oos_r, overlap_lag)

    print("=" * 70)
    print("  DEFINED-RISK IRON CONDOR on NIFTY — net of costs, tail CAPPED")
    print(f"  n={full['n']}  horizon {args.horizon}d  OOS split {str(split)[:10]}")
    print("  (returns = net P&L / capital-at-risk per trade)")
    print("=" * 70)
    print(f"  mean return/trade   {full['mean_pct']:+.1f}% of risk   win {full['win']:.0f}%")
    print(f"  ~Sharpe             {full['sharpe']:.2f}   (overlap-inflated — trust the t-stat)")
    print(f"  t-stat (HAC)        {full['t_stat']:+.2f}   overlap-lag={overlap_lag}   (|t|>2 ≈ real)")
    print(f"  IS  mean            {si['mean_pct']:+.1f}%   t={si['t_stat']:+.2f}   win {si['win']:.0f}%   (n={si['n']})")
    print(f"  OOS mean            {so['mean_pct']:+.1f}%   t={so['t_stat']:+.2f}   win {so['win']:.0f}%   (n={so['n']})")
    print("-" * 70)
    print(f"  worst single trade  {full['worst_pct']:+.1f}% of risk   (CAPPED by the wings)")
    print(f"  max drawdown        {full['mdd_pct']:+.1f}%   (compounding return-on-risk)")
    print("=" * 70)

    edge = si["mean_pct"] > 0 and so["mean_pct"] > 0
    oos_sig = abs(so.get("t_stat", 0.0)) >= 2.0
    print()
    if not oos_sig:
        print(f"  ⚠ OOS t-stat {so['t_stat']:+.2f}: after the overlap correction the OOS")
        print("    return is NOT statistically distinguishable from zero — a positive")
        print("    OOS mean here is within noise. Don't size on it.\n")
    if edge and so["mean_pct"] >= 3:
        print("  VERDICT: NET POSITIVE in BOTH halves after costs, with the tail CAPPED.")
        print("  The VRP is harvestable in defined-risk form. Next: TINY size, forward")
        print(f"  paper 3 months (real fills), then micro-capital. OOS edge {so['mean_pct']:+.1f}%/trade.")
    elif edge:
        print(f"  VERDICT: net positive both halves but THIN OOS ({so['mean_pct']:+.1f}%/trade).")
        print("  Real edge, but the margin after costs is small — live slippage/skew could")
        print("  erase it. Forward paper-test with REAL fills before trusting it.")
    else:
        print(f"  VERDICT: not net-positive in both halves after costs (OOS {so['mean_pct']:+.1f}%).")
        print("  The VRP is real but NOT safely harvestable at retail scale here — the cost")
        print("  of capping the tail eats the thin premium. The honest answer: don't trade")
        print("  it; index investing beats a negative-expectancy 'income' strategy.")
    print("\n  Sweep --delta (0.75-1.25), --wing (0.75-1.5), --cost (0.03-0.10) to see")
    print("  sensitivity. If the edge only appears at unrealistic costs, it isn't real.")


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
