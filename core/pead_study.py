"""
core/pead_study.py — Post-Earnings-Announcement-Drift (PEAD) test harness for
the survivorship-complete mid/small-cap universe (path #2, step 4).

Computes, per earnings event: a market-adjusted (NIFTY-relative) holding-period
return entered AFTER the announcement, net of the liquidity-tiered transaction
cost. Aggregates into the long-beat / short-miss spread that a PEAD strategy
would harvest, and reports the independent-date count so the statistician can
gate significance correctly (the desk's hard lesson: effect size vs independent N).

This harness is data-source-agnostic via injected loaders, so it is unit-testable
without the live archive. Defaults wire to the bhavcopy archive + bar_cache NIFTY
+ the smallcap cost model.

IMPORTANT — this only computes the numbers. The REJECT/PASS verdict is the
statistician's job (date-clustered permutation, multiple-testing correction).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from core.smallcap_costs import estimate_roundtrip_cost, compute_avg_daily_turnover_cr

_ARCHIVE = os.path.join("logs", "bhavcopy_archive")
_BENCHMARK_PATH = os.path.join("logs", "bar_cache", "NIFTY.parquet")


@dataclass
class PeadResult:
    events: pd.DataFrame                      # per-event detail
    n_events: int
    n_distinct_dates: int                     # ← the unit of independence (for power)
    entry_lag: int
    hold_days: int
    by_tercile: Dict[str, dict] = field(default_factory=dict)
    spread_gross: float = float("nan")        # beat mkt-adj − miss mkt-adj
    spread_net: float = float("nan")          # spread minus round-trip cost on both legs
    mean_cost: float = float("nan")


# ── bar helpers ──────────────────────────────────────────────────────────────

def _norm_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Return a date-sorted frame with a 'date' (datetime) and 'close' column."""
    df = bars.copy()
    if "date" not in df.columns:
        df = df.reset_index()
        # the reset index column may be named 'date' or 'index'
        if "date" not in df.columns:
            df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _close_at(df: pd.DataFrame, on_or_after: pd.Timestamp, offset: int):
    """Close of the bar at (first bar on/after `on_or_after`) shifted by `offset`
    trading bars. Returns (date, close) or None if out of range."""
    pos = df["date"].searchsorted(pd.to_datetime(on_or_after), side="left")
    pos += offset
    if pos < 0 or pos >= len(df):
        return None
    row = df.iloc[pos]
    return row["date"], float(row["close"])


def _holding_return(df: pd.DataFrame, announce: pd.Timestamp, entry_lag: int, hold_days: int):
    """(entry_date, exit_date, raw_return) entering `entry_lag` bars after the
    announcement and exiting `hold_days` later. None if insufficient bars."""
    entry = _close_at(df, announce, entry_lag)
    if entry is None:
        return None
    exit_ = _close_at(df, announce, entry_lag + hold_days)
    if exit_ is None:
        return None
    (e_date, e_px), (x_date, x_px) = entry, exit_
    if e_px <= 0:
        return None
    return e_date, x_date, (x_px / e_px) - 1.0


# ── default loaders ──────────────────────────────────────────────────────────

def _archive_loader(symbol: str, archive_dir: str = _ARCHIVE) -> Optional[pd.DataFrame]:
    p = os.path.join(archive_dir, "symbols", f"{symbol}.parquet")
    if not os.path.exists(p):
        return None
    try:
        return _norm_bars(pd.read_parquet(p))
    except Exception:
        return None


def load_benchmark(path: str = _BENCHMARK_PATH) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None
    return _norm_bars(pd.read_parquet(path))


# ── core ─────────────────────────────────────────────────────────────────────

def run_pead(
    events: pd.DataFrame,                     # columns: symbol, date, surprise_pct
    get_bars: Callable[[str], Optional[pd.DataFrame]] = None,
    benchmark: Optional[pd.DataFrame] = None,
    entry_lag: int = 1,                       # enter the close AFTER announcement (not tradeable at pre-announce px)
    hold_days: int = 20,                      # ~1 month drift window
    cost_fn: Callable = None,
    turnover_window: int = 60,
) -> PeadResult:
    """Compute market-adjusted, cost-netted PEAD returns per event and aggregate
    into the beat/miss spread. `events` needs columns symbol, date, surprise_pct."""
    if get_bars is None:
        get_bars = _archive_loader
    if benchmark is None:
        benchmark = load_benchmark()
    if cost_fn is None:
        cost_fn = estimate_roundtrip_cost

    rows = []
    for _, ev in events.iterrows():
        sym = ev["symbol"]
        announce = pd.to_datetime(ev["date"])
        bars = get_bars(sym)
        if bars is None or len(bars) == 0:
            continue
        hr = _holding_return(bars, announce, entry_lag, hold_days)
        if hr is None:
            continue
        e_date, x_date, raw = hr
        # market adjustment over the SAME entry→exit dates
        mkt_adj = raw
        if benchmark is not None:
            b_entry = _close_at(benchmark, e_date, 0)
            b_exit = _close_at(benchmark, x_date, 0)
            if b_entry and b_exit and b_entry[1] > 0:
                mkt_ret = (b_exit[1] / b_entry[1]) - 1.0
                mkt_adj = raw - mkt_ret
        turn = compute_avg_daily_turnover_cr(bars, window=turnover_window, asof=announce)
        cost = cost_fn(sym, turn)
        rows.append({
            "symbol": sym, "announce_date": announce, "surprise_pct": ev.get("surprise_pct"),
            "entry_date": e_date, "exit_date": x_date,
            "raw_ret": raw, "mkt_adj_ret": mkt_adj,
            "turnover_cr": turn, "cost": cost,
            "net_long_ret": mkt_adj - cost,
        })

    edf = pd.DataFrame(rows)
    if len(edf) == 0:
        return PeadResult(events=edf, n_events=0, n_distinct_dates=0,
                          entry_lag=entry_lag, hold_days=hold_days)

    # surprise terciles (beat = top, miss = bottom) over the available population
    edf["tercile"] = "mid"
    valid = edf["surprise_pct"].notna()
    if valid.sum() >= 6:
        q1, q2 = edf.loc[valid, "surprise_pct"].quantile([1/3, 2/3])
        edf.loc[valid & (edf["surprise_pct"] <= q1), "tercile"] = "miss"
        edf.loc[valid & (edf["surprise_pct"] >= q2), "tercile"] = "beat"

    by = {}
    for name, g in edf.groupby("tercile"):
        by[name] = {
            "n": int(len(g)),
            "mean_mkt_adj": float(g["mkt_adj_ret"].mean()),
            "mean_net_long": float(g["net_long_ret"].mean()),
        }

    spread_gross = spread_net = float("nan")
    if "beat" in by and "miss" in by:
        spread_gross = by["beat"]["mean_mkt_adj"] - by["miss"]["mean_mkt_adj"]
        # long-beat / short-miss pays round-trip cost on BOTH legs
        spread_net = spread_gross - 2.0 * float(edf["cost"].mean())

    return PeadResult(
        events=edf, n_events=len(edf),
        n_distinct_dates=int(edf["announce_date"].dt.normalize().nunique()),
        entry_lag=entry_lag, hold_days=hold_days,
        by_tercile=by, spread_gross=spread_gross, spread_net=spread_net,
        mean_cost=float(edf["cost"].mean()),
    )
