"""
core/midcap_universe.py — endogenous, point-in-time, survivorship-complete
mid-cap universe built from the NSE bhavcopy archive.

WHY ENDOGENOUS (not the index constituent list)
------------------------------------------------
The official Nifty Midcap 150 constituent feed is JS-gated and not programmatic;
worse, applying *today's* membership to history is lookahead/survivorship bias
(the very mirage path-#2 exists to avoid). Instead we define the universe from
data we actually have point-in-time: at each rebalance date, rank every equity
that traded by trailing-window turnover, drop the F&O large-caps
(core.universe.FO_UNIVERSE), and take the next liquidity band. Because the
bhavcopy archive contains delisted names until their delisting date, this band
is survivorship-complete and contains no lookahead.

INPUT: logs/bhavcopy_archive/ (built by bhavcopy_archive.py)
  - daily/<YYYYMMDD>.parquet : one row per EQ symbol that traded that day
  - symbols/<SYM>.parquet    : per-symbol OHLCV time series
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from core.universe import FO_UNIVERSE
from core.smallcap_costs import compute_avg_daily_turnover_cr

ARCHIVE_DIR = os.path.join("logs", "bhavcopy_archive")
_FO_SET = {s.upper() for s in FO_UNIVERSE}

# Defaults: the "midcap band" = the top 150 most-liquid names AFTER removing the
# F&O large-caps. Configurable so smallcap bands can be carved too.
DEFAULT_BAND_LO = 1     # 1-indexed rank within the non-F&O set
DEFAULT_BAND_HI = 150
DEFAULT_TURNOVER_WINDOW = 60
DEFAULT_MIN_TURNOVER_CR = 5.0   # drop names too illiquid to trade at all


@dataclass
class UniverseSnapshot:
    asof: str
    symbols: List[str]
    n_candidates: int                 # non-F&O names with enough data
    band: tuple                       # (lo, hi)
    turnover_cr: Dict[str, float] = field(default_factory=dict)


def _symbol_bars(symbol: str, archive_dir: str = ARCHIVE_DIR) -> Optional[pd.DataFrame]:
    """Load a symbol's per-symbol parquet from the archive, or None."""
    path = os.path.join(archive_dir, "symbols", f"{symbol}.parquet")
    if not os.path.exists(path):
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


def _all_symbols(archive_dir: str = ARCHIVE_DIR) -> List[str]:
    paths = glob.glob(os.path.join(archive_dir, "symbols", "*.parquet"))
    return [os.path.splitext(os.path.basename(p))[0] for p in paths]


def build_universe(
    asof: str | date,
    archive_dir: str = ARCHIVE_DIR,
    band_lo: int = DEFAULT_BAND_LO,
    band_hi: int = DEFAULT_BAND_HI,
    turnover_window: int = DEFAULT_TURNOVER_WINDOW,
    min_turnover_cr: float = DEFAULT_MIN_TURNOVER_CR,
    exclude: Optional[set] = None,
) -> UniverseSnapshot:
    """Build the point-in-time mid-cap universe as of `asof`.

    Ranks every non-excluded symbol that has data on/before `asof` by trailing
    turnover, then returns those in rank band [band_lo, band_hi] (1-indexed)
    above `min_turnover_cr`. No lookahead: only bars <= asof are used.
    """
    asof_ts = pd.to_datetime(asof)
    excl = _FO_SET if exclude is None else {s.upper() for s in exclude}

    turnovers: Dict[str, float] = {}
    for sym in _all_symbols(archive_dir):
        if sym.upper() in excl:
            continue
        bars = _symbol_bars(sym, archive_dir)
        if bars is None or len(bars) == 0:
            continue
        t = compute_avg_daily_turnover_cr(bars, window=turnover_window, asof=asof_ts)
        if t >= min_turnover_cr:
            turnovers[sym] = t

    ranked = sorted(turnovers.items(), key=lambda kv: kv[1], reverse=True)
    # 1-indexed inclusive band
    band = ranked[band_lo - 1: band_hi]
    symbols = [s for s, _ in band]
    return UniverseSnapshot(
        asof=str(pd.to_datetime(asof).date()),
        symbols=symbols,
        n_candidates=len(ranked),
        band=(band_lo, band_hi),
        turnover_cr={s: round(t, 2) for s, t in band},
    )


def build_rebalanced(
    start: str | date,
    end: str | date,
    freq: str = "QS",   # quarter-start rebalances
    archive_dir: str = ARCHIVE_DIR,
    **kwargs,
) -> Dict[str, List[str]]:
    """Universe membership over time: {rebalance_date: [symbols]} at each
    rebalance, so a backtest can hold the point-in-time band between dates."""
    dates = pd.date_range(start=start, end=end, freq=freq)
    out: Dict[str, List[str]] = {}
    for d in dates:
        snap = build_universe(d, archive_dir=archive_dir, **kwargs)
        out[str(d.date())] = snap.symbols
    return out
