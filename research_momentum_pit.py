"""
research_momentum_pit.py — the same 12-1 momentum test as
research_momentum_jt.py, but on POINT-IN-TIME bhavcopy data.

WHY A SECOND SCRIPT
-------------------
research_momentum_jt.py runs on data/history, which holds TODAY's F&O names.
Every firm that fell out of the index, got delisted, or collapsed is missing,
so the losers are silently deleted from history. That flatters a
winners-decile strategy more than almost any other design, and it is the
single largest bias in that result (+9.01%/yr, t=1.97).

logs/bhavcopy_archive holds the actual NSE bhavcopy for each trading day: a
name is present while it traded and simply stops appearing when it stops.
Ranking within each day's ACTUAL listed set is therefore survivorship-free.

The point is not to get a better number. It is to find out how much of the
earlier number was survivorship.

LIQUIDITY: bhavcopy is the whole cash market (~1,600 names/day), most of it
untradeable. The universe is filtered to the top N by rupee turnover as of the
FORMATION date -- a point-in-time filter, never a forward-looking one.
"""

from __future__ import annotations

import glob
import os
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(ROOT, "logs", "bhavcopy_archive", "daily")

FORMATION_DAYS = 252
SKIP_DAYS = 21
DECILE = 0.10
TOP_N_LIQUID = 200          # point-in-time liquidity screen
MIN_PRICE = 20.0            # drop penny names: bhavcopy ticks distort returns


def load_pit_panels() -> tuple:
    """Return (close_panel, turnover_panel) from the bhavcopy archive."""
    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    if not files:
        raise SystemExit("no bhavcopy archive - run bhavcopy_archive.py first")
    closes: Dict[pd.Timestamp, pd.Series] = {}
    turns: Dict[pd.Timestamp, pd.Series] = {}
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["symbol", "date", "close", "volume"])
        except Exception:
            continue
        if d.empty:
            continue
        ts = pd.Timestamp(d["date"].iloc[0])
        d = d.drop_duplicates("symbol")
        # Rights entitlements / warrants expire worthless by construction; a
        # ranking that includes them "discovers" an untradeable decay effect.
        from core.corporate_actions import is_tradeable_equity_symbol
        d = d[d["symbol"].map(is_tradeable_equity_symbol)]
        d = d.set_index("symbol")
        closes[ts] = d["close"]
        turns[ts] = d["close"] * d["volume"]
    close = pd.DataFrame(closes).T.sort_index()
    turn = pd.DataFrame(turns).T.sort_index()
    return close, turn


def month_ends(idx: pd.DatetimeIndex) -> List[pd.Timestamp]:
    return list(pd.Series(idx, index=idx).groupby(idx.to_period("M")).last())


def backtest(close: pd.DataFrame, turn: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    dates = month_ends(close.index)
    rows = []
    for i in range(len(dates) - 1):
        form_end, hold_end = dates[i], dates[i + 1]
        loc = close.index.get_loc(form_end)
        skip_loc = loc - SKIP_DAYS
        start_loc = skip_loc - FORMATION_DAYS
        if start_loc < 0:
            continue

        px_now, px_next = close.loc[form_end], close.loc[hold_end]
        p_start, p_skip = close.iloc[start_loc], close.iloc[skip_loc]

        # Point-in-time liquidity: median rupee turnover over the formation
        # window, using only bars at or before the rebalance date.
        tw = turn.iloc[start_loc:loc + 1]
        liq = tw.median()

        alive = (px_now.notna() & px_next.notna() & p_start.notna()
                 & p_skip.notna() & (px_now > MIN_PRICE) & (p_start > 0))
        liq = liq[alive].dropna()
        if len(liq) < 50:
            continue
        universe = liq.nlargest(min(TOP_N_LIQUID, len(liq))).index

        formation = (p_skip[universe] / p_start[universe]) - 1.0
        formation = formation.replace([np.inf, -np.inf], np.nan).dropna()
        # Drop names whose formation window spans a suspected split/bonus: the
        # raw price break is not a return and would rank them as extreme
        # winners or losers. Measured: 296 such events per 400 trading days,
        # including large names (NMDC, MAZDOCK).
        from core.corporate_actions import looks_like_corporate_action
        keep = [s for s in formation.index
                if not looks_like_corporate_action(float(p_start[s]), float(p_skip[s]))
                and not looks_like_corporate_action(float(px_now[s]), float(px_next[s]))]
        formation = formation.loc[keep]
        n = len(formation)
        if n < 20:
            continue

        k = max(1, int(round(n * DECILE)))
        winners = formation.nlargest(k).index
        fwd = float((px_next[winners] / px_now[winners] - 1.0).mean())
        uni = float((px_next[formation.index] / px_now[formation.index] - 1.0).mean())
        rows.append({"date": hold_end, "mom": fwd - cost_bps / 1e4,
                     "uni": uni, "n": n, "held": k})
    return pd.DataFrame(rows)


def stats(r: pd.Series, label: str) -> dict:
    r = r.dropna()
    if r.empty:
        return {}
    ann = (1 + r).prod() ** (12 / len(r)) - 1
    sharpe = (r.mean() / r.std() * np.sqrt(12)) if r.std() > 0 else 0.0
    eq = (1 + r).cumprod()
    return {"label": label, "months": len(r), "ann": ann,
            "vol": r.std() * np.sqrt(12), "sharpe": sharpe,
            "dd": float((eq / eq.cummax() - 1).min())}


def main() -> None:
    close, turn = load_pit_panels()
    print(f"bhavcopy panel: {close.shape[1]:,} symbols ever seen x "
          f"{close.shape[0]:,} days ({close.index[0].date()} .. {close.index[-1].date()})")
    listed = close.notna().sum(axis=1)
    print(f"listed per day: first={listed.iloc[0]:,} last={listed.iloc[-1]:,} "
          f"(a falling count is delisting, i.e. the bias being corrected)")

    from core.charges import round_trip
    statutory = round_trip(1000.0, 1000.0, 500, "futures").pct_of_turnover * 1e4
    slippage = float(os.environ.get("MOM_SLIPPAGE_BPS", "20"))
    cost = statutory + slippage
    print(f"cost: {statutory:.2f} statutory + {slippage:.1f} slippage = {cost:.2f} bps\n")

    m = backtest(close, turn, cost)
    if m.empty:
        raise SystemExit("no rebalances - archive too short for a 12-1 formation")
    m = m.set_index("date")

    rows = [stats(m["mom"], "momentum NET (PIT)"),
            stats(m["uni"], "universe (PIT)")]
    hdr = f"{'strategy':22s} {'mths':>5s} {'ann':>8s} {'vol':>7s} {'Sharpe':>7s} {'maxDD':>8s}"
    print(f"rebalances: {len(m)}  avg ranked: {m['n'].mean():.0f}  held: {m['held'].mean():.0f}\n")
    print(hdr); print("-" * len(hdr))
    for s in rows:
        print(f"{s['label']:22s} {s['months']:5d} {s['ann']*100:7.2f}% "
              f"{s['vol']*100:6.1f}% {s['sharpe']:7.2f} {s['dd']*100:7.1f}%")

    spread = (m["mom"] - m["uni"]).dropna()
    t = spread.mean() / (spread.std() / np.sqrt(len(spread))) if spread.std() > 0 else 0.0
    print(f"\nSPREAD momentum - universe: {spread.mean()*1200:+.2f}%/yr  t={t:.2f}  "
          f"hit={float((spread>0).mean())*100:.1f}%")
    print("Survivorship-free: a name is ranked only while it actually traded.")
    print("|t| < 2 => no edge over the universe once dead firms are included.")


if __name__ == "__main__":
    main()
