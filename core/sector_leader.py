"""
Sector-leader gate.

Premise: in any sector breakout, money flows to the TOP 1-3 strongest names.
Trailing names lag or reverse. Trading a #5 ranked stock in a hot sector
is a B-grade play even with a perfect setup on its own chart.

Logic:
  - For each sector, rank constituent F&O stocks by 20-day return.
  - Long signal accepted only if symbol is in TOP_N of its sector.
  - Short signal accepted only if symbol is in BOTTOM_N of its sector.
  - Sectors with < MIN_SECTOR_SIZE constituents fall back to NIFTY-50 universe
    ranking (so single-stock sectors don't break the gate).
  - Symbol with unknown sector → pass through (degrade gracefully).
  - 1h cache.

Data via yfinance daily bars; reuses SYMBOL_TO_SECTOR map from
sector_rotation.py.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import pandas as pd

from core.sector_rotation import SYMBOL_TO_SECTOR, DEFAULT_SECTOR

log = logging.getLogger(__name__)

# Config
TOP_N_LONG          = 3   # long allowed only if rank ≤ TOP_N_LONG within sector
BOTTOM_N_SHORT      = 3   # short allowed only if rank ≥ (count - BOTTOM_N_SHORT)
LOOKBACK_DAYS_RET   = 20  # 20-day return for ranking
MIN_SECTOR_SIZE     = 4   # fewer constituents → fall back to broad-ranking
CACHE_TTL_SEC       = 3600


# Cache: sector → (timestamp, ranked_symbols_list)
_RANK_CACHE: Dict[str, Tuple[float, List[Tuple[str, float]]]] = {}


def _yf_ticker(sym: str) -> str:
    """Mirror of sector_rotation._yf_ticker (kept local for fewer imports)."""
    remap = {
        "TATAMOTORS": "TMCV.NS",
        "MCDOWELL-N": "UNITDSPR.NS",
        "DEEPAKNT": "DEEPAKNTR.NS",
    }
    if sym in remap:
        return remap[sym]
    return f"{sym}.NS"


def _fetch_20d_return(symbol: str) -> Optional[float]:
    """Return 20-day percent return for a symbol, or None on failure."""
    try:
        import yfinance as yf
        df = yf.download(_yf_ticker(symbol), period="35d", interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty or len(df) < LOOKBACK_DAYS_RET + 1:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df.columns = [c.lower() for c in df.columns]
        close = df["close"].dropna()
        if len(close) < LOOKBACK_DAYS_RET + 1:
            return None
        ret = float(close.iloc[-1] / close.iloc[-(LOOKBACK_DAYS_RET + 1)] - 1)
        return ret
    except Exception as e:
        log.debug(f"[SL] {symbol} return fetch failed: {e}")
        return None


def _build_sector_constituents() -> Dict[str, List[str]]:
    """Invert SYMBOL_TO_SECTOR → sector ticker → list of symbol strings."""
    out: Dict[str, List[str]] = defaultdict(list)
    for sym, sec in SYMBOL_TO_SECTOR.items():
        out[sec].append(sym)
    return dict(out)


_SECTOR_CONSTITUENTS = _build_sector_constituents()


def get_sector_ranking(sector: str) -> List[Tuple[str, float]]:
    """
    Return list of (symbol, 20d_return) sorted DESC by return for the given
    sector ticker. Cached 1h.
    """
    now = time.time()
    cached = _RANK_CACHE.get(sector)
    if cached and (now - cached[0]) < CACHE_TTL_SEC:
        return cached[1]

    members = _SECTOR_CONSTITUENTS.get(sector, [])
    if len(members) < MIN_SECTOR_SIZE:
        # Fallback: use the WHOLE F&O universe in SYMBOL_TO_SECTOR
        members = list(SYMBOL_TO_SECTOR.keys())

    rets: List[Tuple[str, float]] = []
    for sym in members:
        r = _fetch_20d_return(sym)
        if r is not None:
            rets.append((sym, r))
    rets.sort(key=lambda x: x[1], reverse=True)
    _RANK_CACHE[sector] = (now, rets)
    return rets


def _rank_of(symbol: str, ranking: List[Tuple[str, float]]) -> Optional[int]:
    """1-based rank within ranking, or None if not present."""
    for i, (sym, _) in enumerate(ranking):
        if sym == symbol:
            return i + 1
    return None


def check_sector_leader(symbol: str, direction: str
                        ) -> Tuple[bool, Dict]:
    """
    Gate function.
      - direction='long'  : symbol must be in TOP_N_LONG of its sector
      - direction='short' : symbol must be in BOTTOM_N_SHORT of its sector
    Pass-through if sector unknown or ranking empty.
    """
    sector = SYMBOL_TO_SECTOR.get(symbol.upper())
    if sector is None:
        return True, {"reason": "unmapped_sector_pass", "sector": None}

    ranking = get_sector_ranking(sector)
    if not ranking:
        return True, {"reason": "no_ranking_data_pass", "sector": sector}

    rank = _rank_of(symbol.upper(), ranking)
    total = len(ranking)
    info = {
        "sector": sector,
        "rank": rank,
        "total": total,
        "direction": direction,
    }

    if rank is None:
        # Symbol not present in the ranking (data fetch failed for it)
        return True, {**info, "reason": "symbol_no_return_pass"}

    if direction == "long":
        if rank <= TOP_N_LONG:
            return True, {**info,
                          "reason": f"top_{rank}_of_{total}"}
        return False, {**info,
                       "reason": f"rank_{rank}_outside_top_{TOP_N_LONG}"}
    else:  # short
        threshold = max(total - BOTTOM_N_SHORT + 1, 1)
        if rank >= threshold:
            return True, {**info,
                          "reason": f"bottom_{total - rank + 1}_of_{total}"}
        return False, {**info,
                       "reason": f"rank_{rank}_outside_bottom_{BOTTOM_N_SHORT}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Sector constituents:")
    for sec, syms in sorted(_SECTOR_CONSTITUENTS.items()):
        print(f"  {sec:18s} {len(syms):>2d} members")
    print("\nProbe (will hit yfinance for live ranks):")
    for sym in ["HDFCBANK", "INFY", "RELIANCE", "TATAMOTORS"]:
        for d in ("long", "short"):
            ok, info = check_sector_leader(sym, d)
            print(f"  {sym:12s} {d:5s} ok={ok}  {info}")
