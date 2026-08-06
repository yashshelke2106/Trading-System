"""
research_oi_positioning.py — does futures OI positioning predict 1-3 day moves?

STEP 5 OF THE FRAMEWORK, AND THE ONE PIECE NEVER TESTED HERE
------------------------------------------------------------
The classic reading of price against open interest:

    price up   + OI up    -> LONG BUILDUP      (new longs, bullish)
    price down + OI up    -> SHORT BUILDUP     (new shorts, bearish)
    price up   + OI down  -> SHORT COVERING    (shorts closing, weak-bullish)
    price down + OI down  -> LONG UNWINDING    (longs closing, weak-bearish)

This is the most-quoted institutional signal in Indian F&O and the system has
never had the data to check it -- the cash bhavcopy carries no OI. With
fo_bhavcopy_archive.py it can finally be tested rather than assumed.

WHAT WOULD MAKE IT REAL
-----------------------
The four states must separate FORWARD returns, and LONG BUILDUP must beat
SHORT BUILDUP by more than costs. Anything else means the label is describing
the past (which it certainly does) without predicting the future.

RESULT ON THE FULL 2-YEAR ARCHIVE (497 trading days, 2024-08 .. 2026-08)
------------------------------------------------------------------------
    state             hold        n   excess%       t   hit%
    LONG_BUILDUP         3    8,876    -0.049   -1.29   47.7
    SHORT_COVERING       3   18,714    -0.023   -0.98   47.8
    SHORT_BUILDUP        3   10,096    -0.010   -0.29   49.0
    LONG_UNWINDING       3   16,932    +0.023    0.83   49.7

    LONG_BUILDUP minus SHORT_BUILDUP:  1d -0.015%   2d -0.046%   3d -0.039%
    net of 23.6bps costs:              1d -0.251%   2d -0.281%   3d -0.275%

Two things are true at once and both matter:

1. The SIGN is consistently inverted from the textbook -- the "bullish"
   LONG_BUILDUP is the worst state at every horizon and the "bearish"
   LONG_UNWINDING is the best, in both the 163-day and 497-day samples.
2. The MAGNITUDE is not significant. Every |t| < 2 on cells of 8,000-18,000
   observations, so this is small, not underpowered.

An earlier read on 163 days showed LONG_BUILDUP at -0.321% with t=-2.37 and
was flagged preliminary. The full sample cut it ~6x and removed the
significance. Trust this block, not that one -- and treat any short-window
F&O result with the same suspicion.

Practical verdict: the classic OI interpretation carries no tradeable edge,
and acting on it costs about 0.25-0.28% per round trip. Consistent with the
mean-reversion after strong up days already measured twice here (H-017
earnings momentum, and the gap-fade study).

DISCIPLINE
----------
- Near-month contract only, and rolled: OI collapses into expiry, so a raw
  series mixes a dying contract with a newborn one and manufactures fake
  OI changes.
- Forward returns measured on the UNDERLYING (UndrlygPric), not the futures
  close, because that is what an option on the name actually tracks.
- Entry the day AFTER the signal: OI is published post-close.
- Every state is compared to the SAME-DAY cross-sectional mean, so a day when
  everything rose cannot make a state look predictive.
"""

from __future__ import annotations

import glob
import os
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
FUT_DIR = os.path.join(ROOT, "logs", "fo_bhavcopy", "fut")

HOLDS = (1, 2, 3)
MIN_OI_CHANGE_PCT = 1.0     # ignore noise-level OI drift
MIN_PRICE_CHANGE_PCT = 0.5  # ignore flat days; the label needs a direction
COST_BPS = 23.58


def load_futures() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(FUT_DIR, "*.parquet")))
    if not files:
        raise SystemExit("no F&O archive - run fo_bhavcopy_archive.py first")
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception:
            continue
    df = pd.concat(frames, ignore_index=True)
    df["TradDt"] = pd.to_datetime(df["TradDt"], errors="coerce")
    df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce")
    df = df.dropna(subset=["TradDt", "XpryDt", "TckrSymb"])
    return df[df["FinInstrmTp"] == "STF"]        # single-stock futures only


def near_month(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the nearest UNEXPIRED contract per symbol per day.

    Without this the panel mixes contracts and every roll looks like a
    massive OI change that no one actually traded.
    """
    df = df[df["XpryDt"] >= df["TradDt"]]
    idx = df.groupby(["TradDt", "TckrSymb"])["XpryDt"].idxmin()
    return df.loc[idx].copy()


def classify(px_chg: pd.Series, oi_chg: pd.Series) -> pd.Series:
    up_p, up_o = px_chg > 0, oi_chg > 0
    out = pd.Series("NEUTRAL", index=px_chg.index, dtype=object)
    out[up_p & up_o] = "LONG_BUILDUP"
    out[~up_p & up_o] = "SHORT_BUILDUP"
    out[up_p & ~up_o] = "SHORT_COVERING"
    out[~up_p & ~up_o] = "LONG_UNWINDING"
    return out


def main() -> None:
    df = near_month(load_futures())
    print(f"futures rows: {len(df):,}  "
          f"({df['TradDt'].min().date()} .. {df['TradDt'].max().date()})  "
          f"symbols: {df['TckrSymb'].nunique()}")

    spot = df.pivot_table(index="TradDt", columns="TckrSymb",
                          values="UndrlygPric", aggfunc="last").sort_index()
    oi = df.pivot_table(index="TradDt", columns="TckrSymb",
                        values="OpnIntrst", aggfunc="last").sort_index()

    px_chg = spot.pct_change() * 100
    oi_chg = oi.pct_change() * 100

    # Forward underlying returns, entered the day AFTER the signal.
    fwd = {n: (spot.shift(-n - 1) / spot.shift(-1) - 1.0) * 100 for n in HOLDS}
    # Same-day cross-sectional mean = "what everything did", the honest baseline.
    base = {n: fwd[n].mean(axis=1) for n in HOLDS}

    strong = (px_chg.abs() >= MIN_PRICE_CHANGE_PCT) & (oi_chg.abs() >= MIN_OI_CHANGE_PCT)
    state = pd.DataFrame(np.where(strong, "", "NEUTRAL"),
                         index=px_chg.index, columns=px_chg.columns)
    for col in px_chg.columns:
        lab = classify(px_chg[col], oi_chg[col])
        state[col] = np.where(strong[col], lab, "NEUTRAL")

    print(f"\nfilters: |price chg| >= {MIN_PRICE_CHANGE_PCT}%, "
          f"|OI chg| >= {MIN_OI_CHANGE_PCT}%, cost {COST_BPS}bps")
    print("excess% = state's forward return MINUS that day's cross-sectional "
          "mean (so a rising tape cannot flatter a state)\n")

    hdr = (f"{'state':16s} {'hold':>5s} {'n':>8s} {'raw%':>8s} {'excess%':>9s} "
           f"{'t':>7s} {'hit%':>6s}")
    print(hdr); print("-" * len(hdr))

    rows: List[Dict] = []
    for st in ("LONG_BUILDUP", "SHORT_COVERING", "SHORT_BUILDUP", "LONG_UNWINDING"):
        mask = state == st
        for n in HOLDS:
            r = fwd[n].where(mask)
            ex = r.sub(base[n], axis=0).stack().dropna()
            raw = r.stack().dropna()
            if len(ex) < 300:
                continue
            t = ex.mean() / (ex.std() / np.sqrt(len(ex))) if ex.std() > 0 else 0.0
            rows.append({"state": st, "hold": n, "n": len(ex),
                         "raw": raw.mean(), "excess": ex.mean(), "t": t})
            print(f"{st:16s} {n:5d} {len(ex):8,} {raw.mean():8.3f} "
                  f"{ex.mean():9.3f} {t:7.2f} {float((ex>0).mean())*100:6.1f}")

    res = pd.DataFrame(rows)
    if res.empty:
        return
    print("\nTHE TEST THAT MATTERS: LONG_BUILDUP minus SHORT_BUILDUP.")
    print("If OI positioning is informative, the bullish state must beat the")
    print(f"bearish one by more than the {COST_BPS:.1f}bps round trip.\n")
    for n in HOLDS:
        lb = res[(res.state == "LONG_BUILDUP") & (res.hold == n)]
        sb = res[(res.state == "SHORT_BUILDUP") & (res.hold == n)]
        if lb.empty or sb.empty:
            continue
        spread = lb["excess"].iloc[0] - sb["excess"].iloc[0]
        print(f"  {n}d hold: LONG_BUILDUP {lb['excess'].iloc[0]:+.3f}%  "
              f"SHORT_BUILDUP {sb['excess'].iloc[0]:+.3f}%  "
              f"spread {spread:+.3f}%  net {spread - COST_BPS/100:+.3f}%")


if __name__ == "__main__":
    main()
