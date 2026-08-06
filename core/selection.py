"""
core/selection.py — the tradeable universe, chosen by measurement.

WHAT CHANGED AND WHY
--------------------
`core/universe.py` is a hand-maintained list. Hand-maintained lists rot: two
of its names (LTIM, GUJGASLTD) are delisted with no successor and silently
fail every fetch, and four more were renamed by corporate actions. Worse, a
flat list says nothing about HOW liquid each name is, so a Rs 261 Cr/day name
and a Rs 2,322 Cr/day name were treated as interchangeable.

Selection is a measurement, not an opinion. This module reads the ranking
produced by build_top50_liquid.py and hands back the tier appropriate to what
is being traded:

    OPTIONS  >= 500 Cr/day   16 names   spreads stay payable
    FUTURES  >= 300 Cr/day   40 names   1-3 day holds
    RESEARCH >= 260 Cr/day   50 names   studies only, never live orders

The thresholds are not decoration. Options are quoted per strike, so their
spreads widen far faster than the underlying's as turnover falls -- an option
on a Rs 261 Cr name is a different instrument from an option on HDFCBANK.

FALLBACK
--------
If the ranking file is missing (fresh clone, archive not built), this falls
back to core/universe.py MINUS the known-dead names, so callers still work.
The fallback is announced in `source` rather than being silent, because a
caller that thinks it has a measured universe and actually has a hardcoded one
is exactly the kind of quiet wrongness this module exists to remove.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RANKING = os.path.join(ROOT, "data", "top50", "_ranking.csv")

# Turnover floors in Rs crore/day, measured as the MEDIAN daily value.
TIER_OPTIONS = 500.0
TIER_FUTURES = 300.0
TIER_RESEARCH = 260.0

# Present in core/universe.py, absent from Dhan's scrip master AND yfinance,
# with no successor to alias to. See scrip_master.DELISTED_NO_SUCCESSOR.
DEAD = ("LTIM", "GUJGASLTD")

# Below this, turnover is too event-driven to size against its own median.
MIN_STABILITY = 0.55


@dataclass
class Universe:
    symbols: List[str]
    tier: str
    floor_cr: float
    source: str          # "measured" or "fallback:universe.py"

    def __len__(self) -> int:
        return len(self.symbols)

    def __iter__(self):
        return iter(self.symbols)

    def __contains__(self, s) -> bool:
        return str(s).upper() in self.symbols


def _fallback(floor: float, tier: str) -> Universe:
    try:
        from core.universe import FO_UNIVERSE
        syms = [s for s in FO_UNIVERSE if s not in DEAD]
    except Exception:
        syms = []
    return Universe(syms, tier, floor, "fallback:universe.py")


def _load_ranking():
    if not os.path.exists(RANKING):
        return None
    try:
        import pandas as pd
        d = pd.read_csv(RANKING)
        d = d.rename(columns={d.columns[0]: "symbol"})
        if "median_turnover_cr" not in d.columns:
            return None
        return d
    except Exception:
        return None


def universe(tier: str = "futures",
             min_stability: Optional[float] = MIN_STABILITY) -> Universe:
    """Tradeable symbols for `tier`: 'options' | 'futures' | 'research'.

    min_stability drops names whose turnover is burst-driven (p25/median below
    the threshold) -- they are liquid on event days and thin between, so a
    position sized on the median can be hard to exit. Pass None to keep them.
    """
    tier = tier.lower()
    floor = {"options": TIER_OPTIONS, "futures": TIER_FUTURES,
             "research": TIER_RESEARCH}.get(tier)
    if floor is None:
        raise ValueError(f"tier must be options|futures|research, got {tier!r}")

    d = _load_ranking()
    if d is None:
        return _fallback(floor, tier)

    d = d[d["median_turnover_cr"] >= floor]
    if min_stability is not None and "stability" in d.columns:
        d = d[d["stability"] >= min_stability]
    syms = [str(s).upper() for s in d["symbol"] if str(s).upper() not in DEAD]
    if not syms:
        return _fallback(floor, tier)
    return Universe(syms, tier, floor, "measured")


def turnover_of(symbol: str) -> Optional[float]:
    """Median daily turnover in Rs crore, or None if unranked."""
    d = _load_ranking()
    if d is None:
        return None
    row = d[d["symbol"].astype(str).str.upper() == str(symbol).upper()]
    if row.empty:
        return None
    return float(row["median_turnover_cr"].iloc[0])


def is_tradeable(symbol: str, tier: str = "futures") -> bool:
    """Does this name clear the liquidity floor for what you intend to trade?"""
    return str(symbol).upper() in universe(tier).symbols
