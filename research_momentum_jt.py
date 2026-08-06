"""
research_momentum_jt.py — Jegadeesh-Titman (1993) cross-sectional momentum,
tested on this system's 10-year daily archive, net of real Indian charges.

WHY THIS ONE
------------
It is the most-replicated anomaly in the equity literature (Jegadeesh & Titman
1993; Fama-French 1996 call it the one anomaly their 3-factor model cannot
explain; Asness/AQR replicate it across markets and decades). If any published
factor is going to survive on a liquid Indian large-cap universe, this is the
prior favourite. It is also where this project's own research record pointed
after mean-reversion was falsified ("frontier now narrows to momentum/trend").

THE TEXTBOOK SPEC (not tuned here on purpose)
---------------------------------------------
  formation : 12-month return, SKIPPING the most recent month (12-1). The skip
              is not optional decoration -- it sidesteps short-term reversal,
              which is a different and opposing effect.
  ranking   : cross-sectional, all names ranked against each other each month
  portfolio : long the top decile, equal weight
  rebalance : monthly
  costs     : netted with core.charges (futures segment)

WHAT WOULD MAKE THIS REAL
-------------------------
A positive result here is NOT tradeable evidence on its own: one universe, one
parameterisation, survivors-only data (the archive holds today's F&O names, so
firms that fell out are absent -- a known upward bias). Treat any positive as a
reason to run the registry/holdout gates, not as a green light.
"""

from __future__ import annotations

import glob
import os
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, "data", "history")

FORMATION_DAYS = 252     # ~12 months
SKIP_DAYS = 21           # ~1 month, the "-1" in 12-1
DECILE = 0.10


def load_panel(min_bars: int = 600) -> pd.DataFrame:
    """Wide close-price panel: rows = dates, cols = symbols."""
    series: Dict[str, pd.Series] = {}
    for path in glob.glob(os.path.join(HIST, "*.csv")):
        sym = os.path.basename(path)[:-4]
        if sym.startswith("_"):
            continue
        try:
            df = pd.read_csv(path, usecols=["date", "close"])
        except Exception:
            continue
        if len(df) < min_bars:
            continue
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).set_index("date")["close"]
        series[sym] = df[~df.index.duplicated(keep="last")]
    if not series:
        raise SystemExit("no history found - run scripts/build_history_archive first")
    return pd.DataFrame(series).sort_index()


def month_ends(idx: pd.DatetimeIndex) -> List[pd.Timestamp]:
    return list(pd.Series(idx, index=idx).groupby(idx.to_period("M")).last())


def backtest(panel: pd.DataFrame, cost_bps_roundtrip: float) -> dict:
    """Long-top-decile, monthly rebalance. Returns per-month NET returns."""
    dates = month_ends(panel.index)
    rows = []
    for i in range(len(dates) - 1):
        form_end = dates[i]
        hold_end = dates[i + 1]

        # Formation window ends SKIP_DAYS before the rebalance date.
        loc = panel.index.get_loc(form_end)
        skip_loc = loc - SKIP_DAYS
        start_loc = skip_loc - FORMATION_DAYS
        if start_loc < 0:
            continue

        p_start = panel.iloc[start_loc]
        p_skip = panel.iloc[skip_loc]
        formation = (p_skip / p_start) - 1.0

        # Tradeable = priced at both the rebalance and the next month end.
        px_now = panel.loc[form_end]
        px_next = panel.loc[hold_end]
        ok = formation.notna() & px_now.notna() & px_next.notna() & (px_now > 0)
        formation, px_now, px_next = formation[ok], px_now[ok], px_next[ok]
        n = len(formation)
        if n < 20:
            continue

        k = max(1, int(round(n * DECILE)))
        winners = formation.nlargest(k).index
        fwd = (px_next[winners] / px_now[winners] - 1.0).mean()

        # Full turnover assumed each month (conservative: decile membership
        # does churn heavily), so one round trip of cost per rebalance.
        rows.append({
            "date": hold_end,
            "gross": float(fwd),
            "net": float(fwd - cost_bps_roundtrip / 1e4),
            "n": n,
            "held": k,
        })
    return {"months": pd.DataFrame(rows)}


def stats(r: pd.Series, label: str) -> dict:
    r = r.dropna()
    if r.empty:
        return {}
    ann = (1 + r).prod() ** (12 / len(r)) - 1
    vol = r.std() * np.sqrt(12)
    sharpe = (r.mean() / r.std() * np.sqrt(12)) if r.std() > 0 else 0.0
    t = r.mean() / (r.std() / np.sqrt(len(r))) if r.std() > 0 else 0.0
    eq = (1 + r).cumprod()
    dd = float((eq / eq.cummax() - 1).min())
    return {"label": label, "months": len(r), "ann_return": ann, "ann_vol": vol,
            "sharpe": sharpe, "t_stat": t, "max_dd": dd,
            "hit_rate": float((r > 0).mean())}


def main() -> None:
    panel = load_panel()
    print(f"panel: {panel.shape[1]} symbols x {panel.shape[0]} days "
          f"({panel.index[0].date()} .. {panel.index[-1].date()})")

    # Statutory charges ALONE understate a monthly rebalance: the decile churns
    # and every leg crosses a spread. execution._paper_fill already models
    # ~5 bps/side of spread + slippage, and a full round trip on both the exit
    # and the entry is ~4 legs. Defaulting to statutory-only flatters the
    # result into significance (t=2.57 vs 1.49 at a realistic 40 bps), so the
    # slippage term is included by default and can be overridden.
    from core.charges import round_trip
    ch = round_trip(1000.0, 1000.0, 500, "futures")
    statutory_bps = ch.pct_of_turnover * 1e4
    slippage_bps = float(os.environ.get("MOM_SLIPPAGE_BPS", "20"))
    cost_bps = statutory_bps + slippage_bps
    print(f"charge model: {statutory_bps:.2f} bps statutory + {slippage_bps:.1f} bps "
          f"spread/slippage = {cost_bps:.2f} bps round-trip")

    out = backtest(panel, cost_bps)
    m = out["months"]
    if m.empty:
        raise SystemExit("no rebalances produced - check archive depth")

    # Equal-weight all-names benchmark over the same months.
    bench = []
    dates = month_ends(panel.index)
    for i in range(len(dates) - 1):
        a, b = panel.loc[dates[i]], panel.loc[dates[i + 1]]
        ok = a.notna() & b.notna() & (a > 0)
        if ok.sum() >= 20:
            bench.append({"date": dates[i + 1], "r": float((b[ok] / a[ok] - 1).mean())})
    bdf = pd.DataFrame(bench).set_index("date")["r"].reindex(
        pd.DatetimeIndex(m["date"])).dropna()

    rows = [stats(m.set_index("date")["gross"], "momentum GROSS"),
            stats(m.set_index("date")["net"], "momentum NET"),
            stats(bdf, "equal-weight universe")]

    print(f"\nrebalances: {len(m)}  avg names ranked: {m['n'].mean():.0f}  "
          f"held/month: {m['held'].mean():.0f}\n")
    hdr = f"{'strategy':24s} {'mths':>5s} {'ann':>8s} {'vol':>7s} {'Sharpe':>7s} {'t':>6s} {'maxDD':>8s} {'hit':>6s}"
    print(hdr); print("-" * len(hdr))
    for s in rows:
        if not s:
            continue
        print(f"{s['label']:24s} {s['months']:5d} {s['ann_return']*100:7.2f}% "
              f"{s['ann_vol']*100:6.1f}% {s['sharpe']:7.2f} {s['t_stat']:6.2f} "
              f"{s['max_dd']*100:7.1f}% {s['hit_rate']*100:5.1f}%")

    # The t-stat printed per row tests a return against ZERO, which on a
    # long-only equity book mostly measures market beta -- the universe itself
    # compounds. The question that matters is whether momentum beats the
    # universe, so test the monthly SPREAD. Measured 2026-08-06: t=1.49 all
    # months, t=0.62 excluding the 2020-21 melt-up. Not significant.
    net, bench_s = rows[1], rows[2]
    if net and bench_s:
        spread = (m.set_index("date")["net"] - bdf.reindex(m["date"]).values)
        spread = pd.Series(spread).dropna()
        t_spread = (spread.mean() / (spread.std() / np.sqrt(len(spread)))
                    if spread.std() > 0 else 0.0)
        excess = net["ann_return"] - bench_s["ann_return"]
        print(f"\nNET momentum minus equal-weight universe: {excess*100:+.2f}%/yr")
        print(f"  t-stat OF THE SPREAD (the test that matters): {t_spread:.2f}")
        print("  |t| < 2 => the book earns the universe, not an edge over it.")
    print("\nSurvivors-only archive (today's F&O names): results are biased "
          "UPWARD. A positive number is a reason to run the holdout gates, "
          "not a green light.")


if __name__ == "__main__":
    main()
