"""
edge_research.py — HONEST search for a real, a-priori edge.

Discipline (to NOT repeat this system's original sin of curve-fitting):
  * Only well-documented effects with ECONOMIC rationale are tested.
  * Parameters are FIXED at textbook/conventional values. NO grid search,
    NO optimization, NO peeking at results to pick thresholds.
  * Every test is split into first-half / second-half (H1 / H2). An edge that
    only shows up in one half is noise. H2 acts as the held-out check.
  * Net of costs (config.FUT_COST_ROUNDTRIP_PCT).
  * Benchmarked against simply HOLDING NIFTY. In a survivor-biased bull window,
    most "edges" are just beta — beating NIFTY OOS is the real bar.
  * Survivorship is NOT corrected (universe = today's liquid names). This
    INFLATES results, so: a NEGATIVE result here is strong; a positive result
    must be discounted and re-checked on a point-in-time universe before trust.

Hypotheses:
  H1  Cross-sectional momentum (Jegadeesh-Titman): rank universe by 12-1 month
      return, long top third [vs short bottom third], hold 1 month. THE most
      robust documented equity anomaly.
  H2  Absolute trend (time-series momentum): hold a name only while close>SMA200.
      This is essentially the system's own "trend-following" thesis, stripped bare.
  H3  Short-term mean reversion (Connors RSI-2): buy RSI(2)<10 while close>SMA200,
      exit on close>SMA5 or 10-day stop. A non-trend effect (less bull-dependent).

Run:  python edge_research.py            (default ~40 liquid names, 3y)
      python edge_research.py --full --days 1095
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

from backtest_live_pipeline import fetch_daily, DEFAULT_UNIVERSE  # reuse proven fetch

try:
    import config
    COST_RT = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06)) / 100.0
except Exception:
    COST_RT = 0.0006

TRADING_DAYS_MONTH = 21
ANN = 12  # months/yr for annualisation of monthly series


# ───────────────────────────── helpers ──────────────────────────────────────

def _sharpe_monthly(rets: np.ndarray) -> float:
    if len(rets) < 3 or np.std(rets) == 0:
        return 0.0
    return float(np.mean(rets) / np.std(rets) * np.sqrt(ANN))


def _summ(rets: List[float]) -> Dict:
    a = np.array(rets, dtype=float)
    if len(a) == 0:
        return {"n": 0}
    return {
        "n": len(a),
        "mean_mo": float(np.mean(a)) * 100,
        "ann": float(np.mean(a)) * ANN * 100,
        "sharpe": _sharpe_monthly(a),
        "pct_pos": float(np.mean(a > 0)) * 100,
    }


def _verdict(strat_ann: float, strat_sharpe: float, bench_ann: float,
             h2_ann: float) -> str:
    beats = strat_ann > bench_ann + 2.0          # >2%/yr over NIFTY
    stable = h2_ann > 0                           # still positive in held-out half
    if beats and stable and strat_sharpe > 0.7:
        return "POSSIBLE EDGE (discount for survivorship; verify on PIT universe)"
    if strat_ann > bench_ann and stable:
        return "marginal / likely beta — not convincing"
    return "NO EDGE (does not beat NIFTY out-of-sample after costs)"


# ───────────────────────────── data panel ───────────────────────────────────

def build_panel(symbols: List[str], days: int) -> (pd.DataFrame, Optional[pd.Series]):
    print(f"[ER] fetching {len(symbols)} names + NIFTY ({days}d) ...")
    closes = {}
    for s in symbols:
        df = fetch_daily(s, days)
        if df is not None and len(df) > 260:
            closes[s] = df["close"]
    nifty = fetch_daily("NIFTY", days)
    nser = nifty["close"] if nifty is not None else None
    panel = pd.DataFrame(closes).sort_index().ffill()
    print(f"[ER] panel: {panel.shape[1]} names x {panel.shape[0]} days "
          f"({panel.index[0].date()} -> {panel.index[-1].date()})")
    return panel, nser


# ───────────────────────────── H1: X-sectional momentum ─────────────────────

def test_xsec_momentum(panel: pd.DataFrame, nifty: pd.Series) -> None:
    print("\n" + "=" * 70)
    print("  H1  CROSS-SECTIONAL MOMENTUM (12-1, long top third, monthly)")
    print("=" * 70)
    n = len(panel)
    nser = nifty.reindex(panel.index, method="ffill") if nifty is not None else None
    rows = []
    t = 252
    while t + TRADING_DAYS_MONTH < n:
        mom = panel.iloc[t - TRADING_DAYS_MONTH] / panel.iloc[t - 252] - 1   # 12-1
        fwd = panel.iloc[t + TRADING_DAYS_MONTH] / panel.iloc[t] - 1
        valid = mom.notna() & fwd.notna()
        mom2, fwd2 = mom[valid], fwd[valid]
        if len(mom2) >= 6:
            ranked = mom2.sort_values()
            k = max(1, len(ranked) // 3)
            top = ranked.index[-k:]
            bottom = ranked.index[:k]
            long_ret = float(fwd2[top].mean()) - 2 * COST_RT           # monthly turnover
            ls_ret = float(fwd2[top].mean() - fwd2[bottom].mean()) - 4 * COST_RT
            nf = (float(nser.iloc[t + TRADING_DAYS_MONTH] / nser.iloc[t] - 1)
                  if nser is not None else 0.0)
            rows.append((panel.index[t], long_ret, ls_ret, nf))
        t += TRADING_DAYS_MONTH

    if not rows:
        print("  insufficient history.")
        return
    longs = [r[1] for r in rows]
    ls = [r[2] for r in rows]
    nfs = [r[3] for r in rows]
    half = len(rows) // 2
    sL, sLS, sN = _summ(longs), _summ(ls), _summ(nfs)
    h1, h2 = _summ(longs[:half]), _summ(longs[half:])
    print(f"  months tested: {len(rows)}   (H1 first {half}, H2 last {len(rows)-half})")
    print(f"  LONG-ONLY top-third : ann {sL['ann']:+.1f}%  Sharpe {sL['sharpe']:.2f}  "
          f"%+mo {sL['pct_pos']:.0f}")
    print(f"  LONG-SHORT          : ann {sLS['ann']:+.1f}%  Sharpe {sLS['sharpe']:.2f}  "
          f"%+mo {sLS['pct_pos']:.0f}")
    print(f"  NIFTY buy&hold      : ann {sN['ann']:+.1f}%  Sharpe {sN['sharpe']:.2f}")
    print(f"  LONG-ONLY  H1 ann {h1['ann']:+.1f}%  |  H2 (held-out) ann {h2['ann']:+.1f}%")
    print(f"  VERDICT: {_verdict(sL['ann'], sL['sharpe'], sN['ann'], h2['ann'])}")
    print(f"  (long-short is market-neutral; judge it on Sharpe>0.7 & H-stability)")


# ───────────────────────────── H2: absolute trend ──────────────────────────

def test_trend(panel: pd.DataFrame, nifty: pd.Series) -> None:
    print("\n" + "=" * 70)
    print("  H2  ABSOLUTE TREND (long only while close > SMA200, per name)")
    print("=" * 70)
    strat_daily = []     # equal-weight daily strategy return across names
    bh_daily = []
    df = panel
    sma = df.rolling(200).mean()
    rets = df.pct_change()
    pos = (df > sma).astype(float).shift(1)        # yesterday's signal acts today
    # transaction cost on position changes
    switches = pos.diff().abs().fillna(0.0)
    strat = (pos * rets) - switches * COST_RT
    # equal-weight across names each day (ignore NaN)
    strat_ew = strat.mean(axis=1, skipna=True).dropna()
    bh_ew = rets.mean(axis=1, skipna=True).reindex(strat_ew.index)
    nser = (nifty.reindex(panel.index, method="ffill").pct_change().reindex(strat_ew.index)
            if nifty is not None else None)

    def ann_sharpe(s):
        s = s.dropna()
        if len(s) < 30 or s.std() == 0:
            return 0.0, 0.0
        return float(s.mean() * 252 * 100), float(s.mean() / s.std() * np.sqrt(252))

    half = len(strat_ew) // 2
    a_all, sh_all = ann_sharpe(strat_ew)
    a_bh, sh_bh = ann_sharpe(bh_ew)
    a_h2, _ = ann_sharpe(strat_ew.iloc[half:])
    a_nf, sh_nf = ann_sharpe(nser) if nser is not None else (0.0, 0.0)
    print(f"  days: {len(strat_ew)}")
    print(f"  TREND-FILTERED long : ann {a_all:+.1f}%  Sharpe {sh_all:.2f}")
    print(f"  Always-in buy&hold  : ann {a_bh:+.1f}%  Sharpe {sh_bh:.2f}")
    print(f"  NIFTY buy&hold      : ann {a_nf:+.1f}%  Sharpe {sh_nf:.2f}")
    print(f"  TREND H2 (held-out) : ann {a_h2:+.1f}%")
    print(f"  VERDICT: {_verdict(a_all, sh_all, max(a_bh, a_nf), a_h2)}")
    print(f"  (trend-following's job is RISK-ADJUSTED improvement: compare Sharpe to buy&hold)")


# ───────────────────────────── H3: RSI-2 mean reversion ─────────────────────

def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def test_meanrev(panel: pd.DataFrame, udata: Dict[str, pd.DataFrame]) -> None:
    print("\n" + "=" * 70)
    print("  H3  SHORT-TERM MEAN REVERSION (Connors RSI-2 < 10, close>SMA200)")
    print("=" * 70)
    trades = []
    for sym, df in udata.items():
        c = df["close"]
        o = df["open"]
        if len(c) < 220:
            continue
        sma200 = c.rolling(200).mean()
        sma5 = c.rolling(5).mean()
        rsi2 = _rsi(c, 2)
        i = 210
        n = len(c)
        while i < n - 1:
            if rsi2.iloc[i] < 10 and c.iloc[i] > sma200.iloc[i]:
                entry = float(o.iloc[i + 1]) * (1 + COST_RT / 2)   # next open + half cost
                exit_px = None
                for k in range(i + 1, min(i + 11, n)):             # 10-day max hold
                    if c.iloc[k] > sma5.iloc[k]:
                        exit_px = float(c.iloc[k]); break
                if exit_px is None:
                    exit_px = float(c.iloc[min(i + 10, n - 1)])
                exit_px *= (1 - COST_RT / 2)
                trades.append((df.index[i + 1], (exit_px - entry) / entry))
                i = min(i + 10, n)                                  # avoid overlap
            else:
                i += 1
    if not trades:
        print("  no trades.")
        return
    rets = [r for _, r in trades]
    half = len(rets) // 2
    a = np.array(rets)
    wr = float(np.mean(a > 0)) * 100
    pf = a[a > 0].sum() / -a[a < 0].sum() if (a < 0).any() else float("inf")
    exp = float(np.mean(a)) * 100
    h2 = np.array(rets[half:])
    h2_pf = h2[h2 > 0].sum() / -h2[h2 < 0].sum() if (h2 < 0).any() else float("inf")
    print(f"  trades: {len(rets)}   WR {wr:.1f}%   PF {pf:.2f}   exp/trade {exp:+.3f}%")
    print(f"  H2 (held-out) PF {h2_pf:.2f}  WR {float(np.mean(h2>0))*100:.1f}%  n={len(h2)}")
    edge = pf > 1.2 and h2_pf > 1.1 and exp > 0
    print(f"  VERDICT: {'POSSIBLE EDGE (verify PIT + costs on midcaps)' if edge else 'NO / WEAK EDGE'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--limit", type=int, default=40)
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

    print("[ER] !!! survivorship NOT corrected — universe = today's names. Positive")
    print("[ER]     results are OPTIMISTIC; negative results are damning.")
    panel, nifty = build_panel(syms, args.days)
    if panel.shape[1] < 6 or panel.shape[0] < 300:
        print("[ER] FATAL: not enough data (Dhan unreachable, or too few names).")
        return 1
    # udata for per-symbol tests
    udata = {s: pd.DataFrame({"close": panel[s], "open": panel[s]}).dropna()
             for s in panel.columns}

    test_xsec_momentum(panel, nifty)
    test_trend(panel, nifty)
    test_meanrev(panel, udata)

    print("\n" + "=" * 70)
    print("  READ THIS: any 'POSSIBLE EDGE' above is on a SURVIVOR-BIASED, single")
    print("  bull-market window. It is a HYPOTHESIS to validate on a point-in-time")
    print("  universe across >=2 regimes, NOT a green light. 'NO EDGE' is reliable.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
