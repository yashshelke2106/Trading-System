"""
research_short_premium.py — does SELLING option premium pay, after the tail?

WHY THIS TEST, AND WHY NOW
--------------------------
Every directional test in this repo has failed, and the option-buying
arithmetic explains why buying cannot work: an ATM call needs ~0.100%/day of
sustained drift to beat theta while the whole market supplies ~0.0982%/day.
That money does not vanish -- it goes to whoever sold the option. Selling is
the unexplored side of the same coin.

It became testable only once fo_bhavcopy_archive.py existed: 497 trading days
of real per-contract premiums, strikes and OI. Before that there was no
honest way to price a historical short.

WHAT IS ACTUALLY BEING MEASURED
-------------------------------
Sell an OTM strangle (one call above spot, one put below), hold to expiry,
settle at intrinsic against the underlying's expiry price. P&L is therefore
premium collected minus what the option was worth at the end, minus costs.

THE WIN RATE IS NOT THE RESULT. A short OTM strangle wins most months by
construction -- that is what OTM means, not evidence of skill. This repo has
already been caught by exactly that once (H-014 iron condor: 81% wins,
out-of-sample t=0.42). So the outputs that matter here are:

    - mean P&L per trade, net of costs
    - the LEFT TAIL: worst trade, p1, p5, and how much of the total P&L the
      worst 1% destroys
    - return on MARGIN, because a short option ties up SPAN margin and the
      real question is return on capital tied, not return per lot

A strategy that wins 85% of the time and gives it all back in the 15% is a
losing strategy with good marketing.
"""

from __future__ import annotations

import glob
import os
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
OPT_DIR = os.path.join(ROOT, "logs", "fo_bhavcopy", "opt")

OTM_PCT = 5.0            # strike distance from spot, each side
ENTRY_DTE = 20           # sell this many calendar days before expiry
MIN_PREMIUM = 1.0        # ignore untradeable near-zero premiums
MIN_OI = 500             # contract must actually have open interest
COST_PER_LEG_PCT = 0.10  # round-trip cost as % of premium notional, per leg


def load_options() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(OPT_DIR, "*.parquet")))
    if not files:
        raise SystemExit("no option archive - run fo_bhavcopy_archive.py")
    frames = []
    for f in files:
        try:
            d = pd.read_parquet(f)
        except Exception:
            continue
        if d.empty:
            continue
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df["TradDt"] = pd.to_datetime(df["TradDt"], errors="coerce")
    df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce")
    for c in ("StrkPric", "ClsPric", "UndrlygPric", "OpnIntrst"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["TradDt", "XpryDt", "StrkPric", "ClsPric",
                             "UndrlygPric", "OptnTp"])


def build_trades(df: pd.DataFrame) -> pd.DataFrame:
    """One short strangle per (symbol, expiry): sell OTM CE + OTM PE."""
    df = df[(df["OpnIntrst"] >= MIN_OI) & (df["ClsPric"] >= MIN_PREMIUM)]
    df["dte"] = (df["XpryDt"] - df["TradDt"]).dt.days

    # Expiry settlement price of the underlying, per (symbol, expiry).
    settle = (df.sort_values("TradDt")
                .groupby(["TckrSymb", "XpryDt"])["UndrlygPric"].last()
                .rename("settle"))

    # Entry rows: the day closest to ENTRY_DTE before expiry.
    cand = df[(df["dte"] > 0) & (df["dte"] <= ENTRY_DTE + 7)].copy()
    cand["dte_gap"] = (cand["dte"] - ENTRY_DTE).abs()
    entry_day = (cand.groupby(["TckrSymb", "XpryDt"])["dte_gap"].idxmin())
    anchors = cand.loc[entry_day, ["TckrSymb", "XpryDt", "TradDt",
                                   "UndrlygPric"]]
    anchors = anchors.rename(columns={"TradDt": "entry_dt",
                                      "UndrlygPric": "spot"})

    day = cand.merge(anchors, on=["TckrSymb", "XpryDt"])
    day = day[day["TradDt"] == day["entry_dt"]]

    rows: List[Dict] = []
    for (sym, exp), g in day.groupby(["TckrSymb", "XpryDt"]):
        spot = float(g["spot"].iloc[0])
        if spot <= 0:
            continue
        ce = g[(g["OptnTp"] == "CE") & (g["StrkPric"] >= spot * (1 + OTM_PCT / 100))]
        pe = g[(g["OptnTp"] == "PE") & (g["StrkPric"] <= spot * (1 - OTM_PCT / 100))]
        if ce.empty or pe.empty:
            continue
        ce_row = ce.loc[ce["StrkPric"].idxmin()]      # nearest OTM call
        pe_row = pe.loc[pe["StrkPric"].idxmax()]      # nearest OTM put
        try:
            s = float(settle.loc[(sym, exp)])
        except KeyError:
            continue
        if not np.isfinite(s) or s <= 0:
            continue

        # A split or bonus is not a move. Left in, 21 trades (0.41%) -- all of
        # them splits, e.g. BAJFINANCE 9371->951 (1:10), RELIANCE 2743->1332
        # (1:2 bonus), HDFCBANK 1973->957 -- swung the mean from -0.011% to
        # -0.187% and the t-stat from -0.25 to -3.01. The whole conclusion sat
        # on corporate actions read as 80% crashes.
        from core.corporate_actions import looks_like_corporate_action
        if looks_like_corporate_action(spot, s):
            continue

        prem = float(ce_row["ClsPric"]) + float(pe_row["ClsPric"])
        payout = max(0.0, s - float(ce_row["StrkPric"])) + \
                 max(0.0, float(pe_row["StrkPric"]) - s)
        cost = prem * COST_PER_LEG_PCT      # commission+spread on the premium
        pnl = prem - payout - cost

        rows.append({
            "symbol": sym, "expiry": exp, "entry": g["TradDt"].iloc[0],
            "spot": spot, "settle": s, "ce_k": float(ce_row["StrkPric"]),
            "pe_k": float(pe_row["StrkPric"]), "premium": prem,
            "payout": payout, "pnl": pnl,
            "pnl_pct_spot": pnl / spot * 100,
            "move_pct": (s / spot - 1) * 100,
        })
    return pd.DataFrame(rows)


def report(t: pd.DataFrame) -> None:
    n = len(t)
    pnl = t["pnl_pct_spot"]
    wins = (pnl > 0).mean()
    print(f"trades: {n:,}   symbols: {t['symbol'].nunique()}   "
          f"expiries: {t['expiry'].nunique()}")
    print(f"entry ~{ENTRY_DTE}d before expiry, strikes ~{OTM_PCT}% OTM each side\n")

    print(f"WIN RATE            {wins*100:6.2f}%   <- expected to be high; not the result")
    print(f"mean P&L / trade    {pnl.mean():+6.3f}% of spot")
    print(f"median P&L          {pnl.median():+6.3f}%")
    t_stat = pnl.mean() / (pnl.std() / np.sqrt(n)) if pnl.std() > 0 else 0.0
    print(f"t-stat              {t_stat:6.2f}")
    print(f"std dev             {pnl.std():6.3f}%")

    print("\nLEFT TAIL — the part that decides it")
    for q in (0.001, 0.01, 0.05, 0.10):
        print(f"  p{q*100:<5.1f}          {pnl.quantile(q):+7.3f}%")
    print(f"  worst trade      {pnl.min():+7.3f}%   ({t.loc[pnl.idxmin(),'symbol']}, "
          f"move {t.loc[pnl.idxmin(),'move_pct']:+.1f}%)")

    losses = pnl[pnl < 0]
    print(f"\n  avg win          {pnl[pnl>0].mean():+7.3f}%")
    print(f"  avg loss         {losses.mean():+7.3f}%")
    print(f"  worst 1% of trades destroy "
          f"{-pnl.nsmallest(max(1,int(n*0.01))).sum()/abs(pnl.sum())*100 if pnl.sum()!=0 else float('nan'):.0f}% "
          f"of total P&L")

    # Return on margin: a short strangle margins roughly like one futures lot.
    from core.margin import DEFAULT_MARGIN
    margin_frac = (DEFAULT_MARGIN["stock_futures_span"]
                   + DEFAULT_MARGIN["stock_futures_exposure"]) * \
                  (1 + DEFAULT_MARGIN["broker_buffer"])
    ror = pnl.mean() / (margin_frac * 100) * 100
    print(f"\nRETURN ON MARGIN    {ror:+.2f}% per trade "
          f"(margin ~{margin_frac*100:.1f}% of notional)")
    cycles = 252 / ENTRY_DTE
    print(f"  annualised (~{cycles:.0f} cycles/yr): {ror*cycles:+.1f}%/yr")

    print("\nBY YEAR")
    for y, g in t.groupby(t["entry"].dt.year):
        gp = g["pnl_pct_spot"]
        print(f"  {y}  n={len(gp):5,}  mean={gp.mean():+.3f}%  "
              f"win={((gp>0).mean())*100:5.1f}%  worst={gp.min():+.2f}%")


def main() -> None:
    df = load_options()
    print(f"option archive: {len(df):,} rows, "
          f"{df['TradDt'].min().date()} .. {df['TradDt'].max().date()}")
    t = build_trades(df)
    if t.empty:
        raise SystemExit("no qualifying strangles built")
    report(t)
    print("\nA high win rate on OTM shorts is structural, not skill. Judge this "
          "on mean P&L, the left tail, and return on margin.")


if __name__ == "__main__":
    main()
