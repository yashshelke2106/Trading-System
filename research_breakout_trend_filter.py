"""
research_breakout_trend_filter.py — do breakouts pay ONLY when the stock is
already trending, and not when it is chopping sideways?

THE RULE UNDER TEST (user's, 2026-08-06)
----------------------------------------
Entry quality is what matters. Take breakouts on BOTH sides -- upward and
downward -- and always avoid stocks that are moving sideways.

This is a sharper claim than the plain breakout test, which found every arm
net-negative. The new ingredient is the sideways filter: the argument is that
those losses came from range-bound names whipsawing through their own highs,
and that screening them out is what makes the entry good.

HOW "SIDEWAYS" IS MEASURED
--------------------------
Kaufman's Efficiency Ratio over N days:

    ER = |close_t - close_{t-N}| / sum(|daily close changes|)

ER near 1.0 = a clean directional move (every day's travel went somewhere).
ER near 0.0 = the price travelled a lot and arrived nowhere -- sideways chop.
It needs no parameter tuning beyond the window and is not a lagging average of
an average, which is why it is preferred here to ADX.

Breakouts are then split into ER terciles, so the question is not "do
breakouts work" but "does the sideways filter separate the winners from the
losers". If the user's rule is right, the top-ER tercile should be clearly
better than the bottom one.

BOTH SIDES, AND WHY THE SHORT BAR IS HIGHER
-------------------------------------------
Upward breakouts are tested against the long baseline and downward breakdowns
against the short baseline. Measured separately on this same data, an
UNCONDITIONAL short loses about -21.5%/yr purely from the market's upward
drift, so a short breakout must clear that handicap before it means anything.
Comparing a short strategy to zero, rather than to the short baseline, is how
drift gets mistaken for skill.
"""

from __future__ import annotations

import glob
import os
from typing import Dict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(ROOT, "logs", "bhavcopy_archive", "daily")

BREAK_LOOKBACK = 20
ER_WINDOW = 20
HOLDS = (1, 2, 3, 5)
TOP_N_LIQUID = 300
MIN_PRICE = 20.0
COST_BPS = 23.58


def load() -> tuple:
    from core.corporate_actions import is_tradeable_equity_symbol
    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    if not files:
        raise SystemExit("no bhavcopy archive")
    c: Dict = {}
    h: Dict = {}
    lo: Dict = {}
    v: Dict = {}
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["symbol", "date", "close", "high",
                                            "low", "volume"])
        except Exception:
            continue
        if d.empty:
            continue
        ts = pd.Timestamp(d["date"].iloc[0])
        d = d.drop_duplicates("symbol")
        d = d[d["symbol"].map(is_tradeable_equity_symbol)].set_index("symbol")
        c[ts], h[ts], lo[ts], v[ts] = d["close"], d["high"], d["low"], d["volume"]
    return (pd.DataFrame(c).T.sort_index(), pd.DataFrame(h).T.sort_index(),
            pd.DataFrame(lo).T.sort_index(), pd.DataFrame(v).T.sort_index())


def efficiency_ratio(close: pd.DataFrame, n: int) -> pd.DataFrame:
    """Kaufman ER: net directional travel / total travel. 1=trend, 0=chop."""
    net = (close - close.shift(n)).abs()
    total = close.diff().abs().rolling(n).sum()
    return (net / total.replace(0, np.nan)).clip(0, 1)


def main() -> None:
    from core.corporate_actions import flag_panel

    close, high, low, vol = load()
    print(f"panel: {close.shape[1]:,} symbols x {close.shape[0]:,} days "
          f"({close.index[0].date()} .. {close.index[-1].date()})")

    ca = flag_panel(close)
    turnover = (close * vol).rolling(20).median()
    liquid = (turnover.rank(axis=1, ascending=False) <= TOP_N_LIQUID) & (close > MIN_PRICE)
    ok = liquid & ~ca

    er = efficiency_ratio(close, ER_WINDOW)
    # Tercile thresholds computed cross-sectionally per day (point-in-time).
    er_rank = er.where(ok).rank(axis=1, pct=True)

    up = (close > high.shift(1).rolling(BREAK_LOOKBACK).max()) & ok
    dn = (close < low.shift(1).rolling(BREAK_LOOKBACK).min()) & ok

    # Forward returns, entered the day AFTER the signal.
    fwd = {n: (close.shift(-n - 1) / close.shift(-1) - 1.0).mask(ca) for n in HOLDS}

    print(f"ER window {ER_WINDOW}d, breakout {BREAK_LOOKBACK}d, "
          f"top {TOP_N_LIQUID} liquid, cost {COST_BPS:.1f}bps")
    print("ER tercile: LOW = sideways/chop, HIGH = clean trend\n")

    bands = {"LOW (sideways)": er_rank <= 1 / 3,
             "MID": (er_rank > 1 / 3) & (er_rank <= 2 / 3),
             "HIGH (trending)": er_rank > 2 / 3}

    for side, sig, sign in (("UP breakout", up, +1.0), ("DOWN breakdown", dn, -1.0)):
        base_all = {n: (fwd[n][ok] * sign).stack().mean() for n in HOLDS}
        print(f"=== {side} (baseline = same-direction return of all liquid names) ===")
        hdr = (f"{'ER band':18s} {'hold':>5s} {'n':>8s} {'gross%':>8s} {'net%':>8s} "
               f"{'base%':>8s} {'edge%':>8s} {'t':>6s} {'hit%':>6s}")
        print(hdr); print("-" * len(hdr))
        for name, band in bands.items():
            s = (sig & band).fillna(False)
            for n in HOLDS:
                r = (fwd[n] * sign).where(s).stack().dropna()
                if len(r) < 300:
                    continue
                net = r.mean() - COST_BPS / 1e4
                t = r.mean() / (r.std() / np.sqrt(len(r))) if r.std() > 0 else 0.0
                print(f"{name:18s} {n:5d} {len(r):8,} {r.mean()*100:8.3f} "
                      f"{net*100:8.3f} {base_all[n]*100:8.3f} "
                      f"{(r.mean()-base_all[n])*100:8.3f} {t:6.2f} "
                      f"{float((r>0).mean())*100:6.1f}")
        print()

    print("Read edge%, not gross%. Holding anything for the same days earns the")
    print("baseline; a filter is only worth having if it beats that, and the")
    print("user's rule predicts HIGH (trending) should beat LOW (sideways).")


if __name__ == "__main__":
    main()
