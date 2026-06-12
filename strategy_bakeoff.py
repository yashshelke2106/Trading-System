"""
strategy_bakeoff.py — run MULTIPLE candidate strategies through ONE honest
gauntlet and let the DATA pick the best. No tuning, no cherry-picking.

Rules (so the comparison is fair and honest):
  * Every strategy runs on the SAME universe + SAME out-of-sample window
    (last 40%; the first 60% is in-sample, used only for any selection step).
  * Net of costs. Daily strategies annualised x252, monthly x12 (Sharpe is
    comparable across frequencies).
  * The BENCHMARK is just holding NIFTY (the real, free equity edge). A strategy
    is only "worth it" if it beats that on RISK-ADJUSTED return (Sharpe) out of
    sample. Otherwise: buy the index.
  * Fixed textbook params everywhere. NO optimization.

Candidates:
  Buy&hold NIFTY (benchmark)  |  Buy&hold equal-weight basket
  Trend (close>SMA200)        |  Mean-reversion (Connors RSI-2)
  Low-volatility (monthly)    |  Cross-sectional momentum (monthly)
  Pairs / stat-arb (market-neutral, cointegration IS-select / OOS-trade)
  + VRP iron condor (cited from research_iron_condor.py — needs options data)

Run:  python strategy_bakeoff.py --full --limit 120 --days 1460
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
try:
    import config
    COST_RT = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06)) / 100.0
except Exception:
    COST_RT = 0.0006


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def metrics(rets: pd.Series, ppy: int) -> Dict:
    r = rets.dropna()
    if len(r) < 5 or r.std() == 0:
        return {"ann": 0.0, "sharpe": 0.0, "mdd": 0.0, "pos": 0.0, "n": len(r)}
    ann = float(r.mean() * ppy * 100)
    sharpe = float(r.mean() / r.std() * np.sqrt(ppy))
    eq = (1 + r).cumprod()
    mdd = float(((eq - eq.cummax()) / eq.cummax()).min() * 100)
    return {"ann": ann, "sharpe": sharpe, "mdd": mdd,
            "pos": float((r > 0).mean() * 100), "n": len(r)}


# ── strategies (each returns an OOS return series) ──────────────────────────

def s_buyhold(series: pd.Series, oos_start) -> pd.Series:
    return series.pct_change().loc[oos_start:]


def s_ew_basket(panel: pd.DataFrame, oos_start) -> pd.Series:
    return panel.pct_change().mean(axis=1).loc[oos_start:]


def s_trend(panel: pd.DataFrame, oos_start) -> pd.Series:
    sma = panel.rolling(200).mean()
    rets = panel.pct_change()
    pos = (panel > sma).astype(float).shift(1)
    switch = pos.diff().abs().fillna(0.0)
    strat = (pos * rets - switch * COST_RT)
    return strat.mean(axis=1).loc[oos_start:]


def s_meanrev(panel: pd.DataFrame, oos_start) -> pd.Series:
    cols = list(panel.columns)
    idx = panel.index
    pnl = np.zeros((len(idx), len(cols)))
    for ci, c in enumerate(cols):
        px = panel[c].values
        sma200 = panel[c].rolling(200).mean().values
        sma5 = panel[c].rolling(5).mean().values
        rsi2 = _rsi(panel[c], 2).values
        pos = 0
        for i in range(210, len(px) - 1):
            if pos == 0:
                newpos = 1 if (rsi2[i] < 10 and px[i] > sma200[i]) else 0
            else:
                newpos = 0 if px[i] > sma5[i] else pos
            if newpos != pos:
                pnl[i + 1, ci] -= COST_RT
            if newpos != 0 and px[i] > 0:
                pnl[i + 1, ci] += (px[i + 1] / px[i] - 1)
            pos = newpos
    return pd.Series(pnl.mean(axis=1), index=idx).loc[oos_start:]


def _monthly_quantile(panel: pd.DataFrame, oos_start, pick: str) -> pd.Series:
    """pick='lowvol' -> bottom-vol quintile; 'mom' -> top-tercile 12-1 momentum."""
    rets = panel.pct_change()
    out = []
    step = 21
    for t in range(252, len(panel) - step, step):
        d = panel.index[t]
        if d < oos_start:
            continue
        fwd = panel.iloc[t + step] / panel.iloc[t] - 1
        if pick == "lowvol":
            score = rets.iloc[:t].tail(60).std()           # low vol = better
            ranked = score.dropna().sort_values()
            k = max(2, len(ranked) // 5)
            sel = ranked.index[:k]
        else:                                               # momentum 12-1
            score = panel.iloc[t - step] / panel.iloc[t - 252] - 1
            ranked = score.dropna().sort_values()
            k = max(2, len(ranked) // 3)
            sel = ranked.index[-k:]
        sel = [s for s in sel if not np.isnan(fwd.get(s, np.nan))]
        if len(sel) >= 3:
            out.append(float(fwd[sel].mean()) - 2 * COST_RT)
    return pd.Series(out)


def s_pairs(panel: pd.DataFrame, cut: int) -> Optional[pd.Series]:
    try:
        from pairs_program import select_pairs, pair_daily
    except Exception:
        return None
    pc_is, pc_oos = panel.iloc[:cut], panel.iloc[cut:]
    pairs = select_pairs(pc_is)
    if not pairs:
        return None
    series = []
    for a, b, beta, hl in pairs:
        d, _tr = pair_daily(pc_oos, a, b, beta, 2 * COST_RT)
        if len(d):
            series.append(d.rename(f"{a}_{b}"))
    if not series:
        return None
    return pd.concat(series, axis=1).fillna(0.0).mean(axis=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--limit", type=int, default=120)
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

    print(f"[BAKE] fetching {len(syms)} names + NIFTY ({args.days}d) ...")
    C = {}
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is not None and len(df) > 300:
            df = df[~df.index.duplicated(keep="last")]
            C[s] = df["close"]
    panel = pd.DataFrame(C).sort_index().ffill().dropna(how="all").dropna(axis=1)
    nifty = fetch_daily("NIFTY", args.days)
    nifty = nifty[~nifty.index.duplicated(keep="last")].sort_index()["close"] if nifty is not None else None
    if panel.shape[1] < 8 or nifty is None:
        print("[BAKE] FATAL: not enough data."); return 1
    n = len(panel); cut = int(n * 0.60)
    oos_start = panel.index[cut]
    nifty = nifty.reindex(panel.index, method="ffill")
    print(f"[BAKE] {panel.shape[1]} names x {n} days. "
          f"OOS window: {oos_start.date()} -> {panel.index[-1].date()} (last 40%)\n")

    rows = []
    rows.append(("Buy&hold NIFTY (benchmark)", "daily", metrics(s_buyhold(nifty, oos_start), 252)))
    rows.append(("Buy&hold equal-weight basket", "daily", metrics(s_ew_basket(panel, oos_start), 252)))
    rows.append(("Trend (close>SMA200)", "daily", metrics(s_trend(panel, oos_start), 252)))
    rows.append(("Mean-reversion (RSI-2)", "daily", metrics(s_meanrev(panel, oos_start), 252)))
    rows.append(("Low-volatility (monthly)", "monthly", metrics(_monthly_quantile(panel, oos_start, "lowvol"), 12)))
    rows.append(("Cross-sec momentum (monthly)", "monthly", metrics(_monthly_quantile(panel, oos_start, "mom"), 12)))
    pairs_ser = s_pairs(panel, cut)
    if pairs_ser is not None:
        rows.append(("Pairs / stat-arb (mkt-neutral)", "daily", metrics(pairs_ser, 252)))

    bench = rows[0][2]
    print("=" * 86)
    print("  STRATEGY BAKE-OFF — OUT-OF-SAMPLE, net of cost, vs holding NIFTY")
    print("=" * 86)
    print(f"  {'strategy':32} {'freq':7} {'ann%':>7} {'Sharpe':>7} {'maxDD%':>7} "
          f"{'%+':>5}  verdict")
    print("-" * 86)
    # sort by Sharpe desc (benchmark stays labelled)
    for name, freq, m in sorted(rows, key=lambda r: r[2]["sharpe"], reverse=True):
        if "benchmark" in name:
            verdict = "<-- the bar to beat"
        elif m["sharpe"] > bench["sharpe"] and m["ann"] > 0:
            verdict = "BEATS index (risk-adj)"
        elif m["ann"] > 0:
            verdict = "positive but worse than index"
        else:
            verdict = "NEGATIVE"
        print(f"  {name:32} {freq:7} {m['ann']:>+6.1f} {m['sharpe']:>7.2f} "
              f"{m['mdd']:>7.1f} {m['pos']:>5.0f}  {verdict}")
    print("-" * 86)
    print("  Cited (needs options data, not in this daily run):")
    print("    VRP iron condor (research_iron_condor.py): OOS ~ -1.2% / trade net -> NEGATIVE")
    print("=" * 86)

    # recommendation
    beaters = [(nm, m) for nm, fr, m in rows
               if "benchmark" not in nm and m["sharpe"] > bench["sharpe"] and m["ann"] > 0]
    print("\n  RECOMMENDATION")
    if beaters:
        best = max(beaters, key=lambda x: x[1]["sharpe"])
        print(f"  Best OOS risk-adjusted: '{best[0]}' (Sharpe {best[1]['sharpe']:.2f} vs "
              f"NIFTY {bench['sharpe']:.2f}).")
        print("  This BEAT the index out-of-sample here — but it is survivor-biased and")
        print("  single-regime. Treat as a PAPER candidate: re-test on a point-in-time")
        print("  universe + a drawdown regime before ANY real capital.")
    else:
        print("  NOTHING beat simply holding NIFTY on a risk-adjusted, out-of-sample,")
        print("  cost-net basis. The honest 'best strategy' here is the index itself")
        print("  (capture it via a low-cost fund). Do not trade the others with real money.")
    print("=" * 86)
    return 0


if __name__ == "__main__":
    sys.exit(main())
