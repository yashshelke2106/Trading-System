"""
build_top50_liquid.py — the 50 most liquid F&O names, and WHEN each is liquid.

WHAT IT PRODUCES
----------------
    data/top50/_ranking.csv        50 names ranked by median daily turnover
    data/top50/_monthly.csv        turnover by (symbol, calendar month)
    data/top50/_seasonality.csv    each name's best/worst months, sorted
    data/top50/<SYMBOL>.csv        filtered daily OHLCV history per name

WHY TURNOVER AND NOT VOLUME
---------------------------
Volume in shares is not comparable across names -- 10 lakh shares of a Rs 200
stock and of a Rs 4,000 stock are different businesses. Rupee turnover
(close x volume) is the only cross-comparable liquidity measure, and it is what
decides slippage on an actual order.

MEDIAN, NOT MEAN
----------------
A single delivery-heavy or news day can double a name's average turnover for a
year. The median describes the day you will actually trade on; the mean
describes the day you will not.

LIQUIDITY SEASONALITY
---------------------
Monthly turnover is normalised against each symbol's OWN annual median, so the
output says "this name trades 18% above its own normal in March" rather than
"this name is bigger than that one" -- which the ranking already covers.

Note before reading too much into the monthly numbers: turnover is dominated
by market-wide activity, so most names share the same busy months. The column
that matters is the deviation from a symbol's own baseline, not the raw level.

CORPORATE ACTIONS
-----------------
Split/bonus days are excluded: a 1:10 split multiplies share volume tenfold
overnight and would otherwise look like a permanent liquidity regime change.
"""

from __future__ import annotations

import glob
import os
from typing import Dict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(ROOT, "logs", "bhavcopy_archive", "daily")
OUT = os.path.join(ROOT, "data", "top50")

TOP_N = 50
MIN_DAYS = 400            # must have real history, not a recent listing
LOOKBACK_DAYS = 750       # ~3 years for the ranking


def load_archive() -> pd.DataFrame:
    """Long-format frame: symbol, date, ohlcv, turnover."""
    from core.corporate_actions import is_tradeable_equity_symbol

    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    if not files:
        raise SystemExit("no bhavcopy archive")
    files = files[-LOOKBACK_DAYS:]

    frames = []
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["symbol", "date", "open", "high",
                                            "low", "close", "prev_close",
                                            "volume"])
        except Exception:
            continue
        if d.empty:
            continue
        d = d.drop_duplicates("symbol")
        d = d[d["symbol"].map(is_tradeable_equity_symbol)]
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close", "volume"])
    df["turnover_cr"] = df["close"] * df["volume"] / 1e7      # rupees -> crore
    return df


def rank_liquidity(df: pd.DataFrame) -> pd.DataFrame:
    from core.corporate_actions import looks_like_corporate_action

    df = df.copy()
    df["is_ca"] = [looks_like_corporate_action(p, c)
                   for p, c in zip(df["prev_close"].fillna(0), df["close"])]
    clean = df[~df["is_ca"]]

    g = clean.groupby("symbol")
    stats = pd.DataFrame({
        "days": g["turnover_cr"].size(),
        "median_turnover_cr": g["turnover_cr"].median(),
        "mean_turnover_cr": g["turnover_cr"].mean(),
        "p25_turnover_cr": g["turnover_cr"].quantile(0.25),
        "last_close": g["close"].last(),
    })
    stats = stats[stats["days"] >= MIN_DAYS]

    # Consistency: how often the name clears its own median. A name that is
    # liquid in bursts is not the same as one that is liquid every day.
    stats["stability"] = (stats["p25_turnover_cr"] /
                          stats["median_turnover_cr"]).round(3)

    # Prefer names that are ALSO in the F&O universe -- options need a contract.
    try:
        from core.universe import FO_UNIVERSE
        fo = set(FO_UNIVERSE)
    except Exception:
        fo = set()
    stats["in_fo"] = [s in fo for s in stats.index]

    return stats.sort_values("median_turnover_cr", ascending=False)


def monthly_profile(df: pd.DataFrame, symbols) -> pd.DataFrame:
    sub = df[df["symbol"].isin(symbols)].copy()
    sub["month"] = sub["date"].dt.month
    m = (sub.groupby(["symbol", "month"])["turnover_cr"]
            .median().rename("turnover_cr").reset_index())
    base = m.groupby("symbol")["turnover_cr"].median().rename("own_median")
    m = m.merge(base, on="symbol")
    m["vs_own_pct"] = (m["turnover_cr"] / m["own_median"] - 1) * 100
    return m


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    print("loading archive ...", flush=True)
    df = load_archive()
    print(f"  {df['symbol'].nunique():,} symbols, "
          f"{df['date'].min().date()} .. {df['date'].max().date()}")

    stats = rank_liquidity(df)
    fo_first = stats[stats["in_fo"]].head(TOP_N)
    if len(fo_first) < TOP_N:                     # top up if F&O list is short
        rest = stats[~stats["in_fo"]].head(TOP_N - len(fo_first))
        fo_first = pd.concat([fo_first, rest])
    top = fo_first.head(TOP_N)
    top.insert(0, "rank", range(1, len(top) + 1))
    top.to_csv(os.path.join(OUT, "_ranking.csv"))

    print(f"\nTOP {TOP_N} BY MEDIAN DAILY TURNOVER (Rs crore)")
    print(f"{'#':>3s} {'symbol':<14s} {'median':>9s} {'p25':>9s} "
          f"{'stability':>10s} {'days':>6s}")
    print("-" * 56)
    for i, (sym, r) in enumerate(top.iterrows(), 1):
        print(f"{i:3d} {sym:<14s} {r['median_turnover_cr']:9.1f} "
              f"{r['p25_turnover_cr']:9.1f} {r['stability']:10.2f} "
              f"{int(r['days']):6d}")

    # Monthly seasonality
    m = monthly_profile(df, top.index)
    m.to_csv(os.path.join(OUT, "_monthly.csv"), index=False)

    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    seas = []
    for sym, g in m.groupby("symbol"):
        g = g.set_index("month")["vs_own_pct"]
        best = g.idxmax()
        worst = g.idxmin()
        seas.append({"symbol": sym, "best_month": names[best - 1],
                     "best_vs_own_pct": round(g.loc[best], 1),
                     "worst_month": names[worst - 1],
                     "worst_vs_own_pct": round(g.loc[worst], 1),
                     "swing_pct": round(g.loc[best] - g.loc[worst], 1)})
    seas = pd.DataFrame(seas).sort_values("swing_pct", ascending=False)
    seas.to_csv(os.path.join(OUT, "_seasonality.csv"), index=False)

    print("\nLIQUIDITY SEASONALITY — sorted by how much the name swings")
    print(f"{'symbol':<14s} {'best':>5s} {'+%':>7s} {'worst':>6s} {'-%':>7s} {'swing':>7s}")
    print("-" * 52)
    for _, r in seas.head(15).iterrows():
        print(f"{r['symbol']:<14s} {r['best_month']:>5s} "
              f"{r['best_vs_own_pct']:+7.1f} {r['worst_month']:>6s} "
              f"{r['worst_vs_own_pct']:+7.1f} {r['swing_pct']:7.1f}")

    # Market-wide month pattern, so a symbol's swing can be read against it.
    mkt = m.groupby("month")["vs_own_pct"].median()
    print("\nMARKET-WIDE monthly turnover (median across the 50, vs own baseline)")
    print("  " + "  ".join(f"{names[i-1]}:{mkt.get(i, np.nan):+5.1f}%"
                           for i in range(1, 13)))

    # Per-symbol filtered history
    print(f"\nwriting per-symbol history to {OUT} ...", flush=True)
    written = 0
    for sym in top.index:
        s = (df[df["symbol"] == sym]
             .sort_values("date")[["date", "open", "high", "low", "close",
                                   "volume", "turnover_cr"]])
        if s.empty:
            continue
        s.to_csv(os.path.join(OUT, f"{sym}.csv"), index=False)
        written += 1

    print(f"\nDONE  {written} symbol files")
    print(f"  ranking     : {os.path.join(OUT, '_ranking.csv')}")
    print(f"  monthly     : {os.path.join(OUT, '_monthly.csv')}")
    print(f"  seasonality : {os.path.join(OUT, '_seasonality.csv')}")


if __name__ == "__main__":
    main()
