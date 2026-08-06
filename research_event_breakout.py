"""
research_event_breakout.py — do 1-3 day holds after an EVENT pay?

THE IDEA UNDER TEST (user's, 2026-08-06)
----------------------------------------
Stocks that break a range, or that have something happen to them -- results,
merger, split, rights, buyback, dividend, bonus, capital reduction -- move,
and the move can be captured on a 1 to 3 day hold.

HOW "EVENT" IS DEFINED HERE, AND WHY
------------------------------------
There is no corporate-action calendar in this repo, so rather than assume one,
events are identified by their OBSERVABLE SIGNATURE in the tape. Every action
on that list -- an earnings surprise, a buyback announcement, a bonus -- shows
up the same way: an unusual volume day with an unusual price move. That is
what the market reacting to information looks like, whatever the cause.

This is a fair proxy in one direction and a limitation in the other: it
catches events that MOVED the price (including ones no calendar lists, like a
sector shock), but it cannot separate "results" from "merger". If the signature
pays, the next step is a real action calendar to find WHICH cause pays.

THREE ARMS, deliberately separated because they are different claims:
  BREAKOUT   : close breaks the N-day high, no volume condition
  VOLUME     : volume spike vs its own average, no breakout condition
  BOTH       : the conjunction -- the "news + breakout" case

DATA DISCIPLINE
---------------
Point-in-time bhavcopy (a name is present only while it traded), rights
entitlements removed, suspected splits/bonuses neutralised -- without that
last step a 1:3 bonus reads as a -68% "return" and swamps everything.
Entry is the NEXT day's open-proxy (previous close), never the event close,
because the event day is only known after it has closed.
"""

from __future__ import annotations

import glob
import os
from typing import Dict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(ROOT, "logs", "bhavcopy_archive", "daily")

LOOKBACK_HIGH = 20        # range for the breakout test
VOL_WINDOW = 20
VOL_MULT = 3.0            # "unusual" volume = 3x its own 20d average
MIN_PRICE = 20.0
TOP_N_LIQUID = 300        # point-in-time liquidity screen
HOLDS = (1, 2, 3)
COST_BPS = 23.58          # statutory + slippage, same as the momentum study


def load_panels(limit_days: int = 0) -> tuple:
    from core.corporate_actions import is_tradeable_equity_symbol
    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    if limit_days:
        files = files[-limit_days:]
    if not files:
        raise SystemExit("no bhavcopy archive")
    c: Dict[pd.Timestamp, pd.Series] = {}
    v: Dict[pd.Timestamp, pd.Series] = {}
    h: Dict[pd.Timestamp, pd.Series] = {}
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["symbol", "date", "close",
                                            "high", "volume"])
        except Exception:
            continue
        if d.empty:
            continue
        ts = pd.Timestamp(d["date"].iloc[0])
        d = d.drop_duplicates("symbol")
        d = d[d["symbol"].map(is_tradeable_equity_symbol)].set_index("symbol")
        c[ts], v[ts], h[ts] = d["close"], d["volume"], d["high"]
    close = pd.DataFrame(c).T.sort_index()
    vol = pd.DataFrame(v).T.sort_index()
    high = pd.DataFrame(h).T.sort_index()
    return close, vol, high


def run(close: pd.DataFrame, vol: pd.DataFrame, high: pd.DataFrame) -> pd.DataFrame:
    from core.corporate_actions import flag_panel

    ca = flag_panel(close)                    # suspected split/bonus days
    ret1 = (close / close.shift(1) - 1.0).mask(ca)

    turnover = (close * vol).rolling(20).median()
    liq_rank = turnover.rank(axis=1, ascending=False)
    liquid = (liq_rank <= TOP_N_LIQUID) & (close > MIN_PRICE)

    prior_high = high.shift(1).rolling(LOOKBACK_HIGH).max()
    breakout = (close > prior_high) & liquid & ~ca

    vol_avg = vol.shift(1).rolling(VOL_WINDOW).mean()
    vol_spike = (vol > VOL_MULT * vol_avg) & liquid & ~ca

    arms = {"BREAKOUT": breakout & ~vol_spike,
            "VOLUME": vol_spike & ~breakout,
            "BOTH": breakout & vol_spike}

    # Forward returns from the day AFTER the event (entry at next close).
    fwd = {n: (close.shift(-n - 1) / close.shift(-1) - 1.0).mask(ca)
           for n in HOLDS}
    base = {n: fwd[n][liquid].stack().mean() for n in HOLDS}

    rows = []
    for name, sig in arms.items():
        s = sig.fillna(False)
        for n in HOLDS:
            r = fwd[n].where(s).stack().dropna()
            if len(r) < 200:
                continue
            net = r.mean() - COST_BPS / 1e4
            t = r.mean() / (r.std() / np.sqrt(len(r))) if r.std() > 0 else 0.0
            rows.append({
                "arm": name, "hold_d": n, "n": len(r),
                "gross_%": r.mean() * 100, "net_%": net * 100,
                "baseline_%": base[n] * 100,
                "edge_vs_base_%": (r.mean() - base[n]) * 100,
                "t": t, "hit_%": float((r > 0).mean()) * 100,
            })
    return pd.DataFrame(rows)


def main() -> None:
    close, vol, high = load_panels()
    print(f"panel: {close.shape[1]:,} symbols x {close.shape[0]:,} days "
          f"({close.index[0].date()} .. {close.index[-1].date()})")
    print(f"event defs: breakout={LOOKBACK_HIGH}d high, volume>{VOL_MULT}x{VOL_WINDOW}d avg, "
          f"top {TOP_N_LIQUID} liquid, cost {COST_BPS:.1f}bps\n")

    res = run(close, vol, high)
    if res.empty:
        raise SystemExit("no qualifying events")

    hdr = (f"{'arm':10s} {'hold':>5s} {'n':>8s} {'gross%':>8s} {'net%':>8s} "
           f"{'base%':>8s} {'edge%':>8s} {'t':>7s} {'hit%':>6s}")
    print(hdr); print("-" * len(hdr))
    for _, r in res.iterrows():
        print(f"{r['arm']:10s} {int(r['hold_d']):5d} {int(r['n']):8,} "
              f"{r['gross_%']:8.3f} {r['net_%']:8.3f} {r['baseline_%']:8.3f} "
              f"{r['edge_vs_base_%']:8.3f} {r['t']:7.2f} {r['hit_%']:6.1f}")

    print("\nnet% is after 23.6bps of costs. edge% is versus the same-horizon "
          "return of the whole liquid universe -- the number that matters, "
          "since holding anything for 1-3 days earns the market's drift.")
    best = res.loc[res["net_%"].idxmax()]
    print(f"\nbest net arm: {best['arm']} @ {int(best['hold_d'])}d = "
          f"{best['net_%']:+.3f}% per trade (t={best['t']:.2f})")
    print("A positive gross that turns negative after costs is the usual "
          "outcome for short-hold event studies; check net%, not gross%.")


if __name__ == "__main__":
    main()
