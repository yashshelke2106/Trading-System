"""
Sector rotation gate — block trades against sector trend.

Premise: in Indian markets, sector momentum dominates individual stock noise.
HDFCBANK long in a falling BANK index is a low-probability bet even if the
stock itself prints a clean setup. Most retail systems ignore this — institutional
flow is sector-rotational.

Logic:
  - Every F&O stock maps to a sector index ticker (^NSEBANK, ^CNXIT, etc.)
  - Sector is bullish if: close > EMA20 AND EMA20 slope > 0 over last 5d
  - Long signal: requires sector bullish OR neutral
  - Short signal: requires sector bearish OR neutral
  - Pure counter-sector trades blocked

Sector data via yfinance (free, daily bars). Cached 1h per sector index.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple

import pandas as pd

log = logging.getLogger(__name__)

# ── Symbol → Sector index ticker (yfinance) ──────────────────────────────
# Sources: NSE sectoral indices, Nifty subsector official mappings.
# Not exhaustive — unknown symbols fall back to ^NSEI (broad market gate).
SYMBOL_TO_SECTOR: Dict[str, str] = {
    # Banks (NIFTY BANK)
    "HDFCBANK": "^NSEBANK", "ICICIBANK": "^NSEBANK", "SBIN": "^NSEBANK",
    "KOTAKBANK": "^NSEBANK", "AXISBANK": "^NSEBANK", "INDUSINDBK": "^NSEBANK",
    "FEDERALBNK": "^NSEBANK", "BANKBARODA": "^NSEBANK", "PNB": "^NSEBANK",
    "AUBANK": "^NSEBANK", "IDFCFIRSTB": "^NSEBANK", "BANDHANBNK": "^NSEBANK",
    # IT (NIFTY IT)
    "INFY": "^CNXIT", "TCS": "^CNXIT", "WIPRO": "^CNXIT", "TECHM": "^CNXIT",
    "HCLTECH": "^CNXIT", "LTIM": "^CNXIT", "MPHASIS": "^CNXIT", "PERSISTENT": "^CNXIT",
    "COFORGE": "^CNXIT", "OFSS": "^CNXIT",
    # Auto (NIFTY AUTO)
    "MARUTI": "^CNXAUTO", "TATAMOTORS": "^CNXAUTO", "M&M": "^CNXAUTO",
    "BAJAJ-AUTO": "^CNXAUTO", "HEROMOTOCO": "^CNXAUTO", "EICHERMOT": "^CNXAUTO",
    "ASHOKLEY": "^CNXAUTO", "TVSMOTOR": "^CNXAUTO", "MOTHERSON": "^CNXAUTO",
    "BHARATFORG": "^CNXAUTO", "BOSCHLTD": "^CNXAUTO", "MRF": "^CNXAUTO",
    # Pharma (NIFTY PHARMA)
    "SUNPHARMA": "^CNXPHARMA", "DRREDDY": "^CNXPHARMA", "CIPLA": "^CNXPHARMA",
    "DIVISLAB": "^CNXPHARMA", "LUPIN": "^CNXPHARMA", "AUROPHARMA": "^CNXPHARMA",
    "TORNTPHARM": "^CNXPHARMA", "BIOCON": "^CNXPHARMA", "ZYDUSLIFE": "^CNXPHARMA",
    "ALKEM": "^CNXPHARMA", "GRANULES": "^CNXPHARMA",
    # FMCG (NIFTY FMCG)
    "HINDUNILVR": "^CNXFMCG", "ITC": "^CNXFMCG", "NESTLEIND": "^CNXFMCG",
    "BRITANNIA": "^CNXFMCG", "DABUR": "^CNXFMCG", "GODREJCP": "^CNXFMCG",
    "MARICO": "^CNXFMCG", "TATACONSUM": "^CNXFMCG", "COLPAL": "^CNXFMCG",
    "MCDOWELL-N": "^CNXFMCG", "UBL": "^CNXFMCG",
    # Metals (NIFTY METAL)
    "TATASTEEL": "^CNXMETAL", "HINDALCO": "^CNXMETAL", "JSWSTEEL": "^CNXMETAL",
    "VEDL": "^CNXMETAL", "COALINDIA": "^CNXMETAL", "NMDC": "^CNXMETAL",
    "JINDALSTEL": "^CNXMETAL", "SAIL": "^CNXMETAL", "HINDZINC": "^CNXMETAL",
    "NATIONALUM": "^CNXMETAL", "RATNAMANI": "^CNXMETAL",
    # Energy / Oil-Gas
    "RELIANCE": "^CNXENERGY", "ONGC": "^CNXENERGY", "BPCL": "^CNXENERGY",
    "IOC": "^CNXENERGY", "HPCL": "^CNXENERGY", "GAIL": "^CNXENERGY",
    "PETRONET": "^CNXENERGY", "OIL": "^CNXENERGY", "ADANIPOWER": "^CNXENERGY",
    "TATAPOWER": "^CNXENERGY",
    # Financial Services
    "BAJFINANCE": "^CNXFIN", "BAJAJFINSV": "^CNXFIN", "HDFCLIFE": "^CNXFIN",
    "ICICIPRULI": "^CNXFIN", "SBILIFE": "^CNXFIN", "LICI": "^CNXFIN",
    "CHOLAFIN": "^CNXFIN", "MANAPPURAM": "^CNXFIN", "MUTHOOTFIN": "^CNXFIN",
    "MFSL": "^CNXFIN", "PFC": "^CNXFIN", "RECLTD": "^CNXFIN", "IDFC": "^CNXFIN",
    # PSU (NIFTY PSE)
    "BHEL": "^CNXPSUBANK", "BEL": "^CNXPSE", "NTPC": "^CNXPSE",
    "POWERGRID": "^CNXPSE", "GAIL": "^CNXPSE",
    # Realty (NIFTY REALTY)
    "DLF": "^CNXREALTY", "GODREJPROP": "^CNXREALTY", "OBEROIRLTY": "^CNXREALTY",
    "PRESTIGE": "^CNXREALTY", "BRIGADE": "^CNXREALTY", "PHOENIXLTD": "^CNXREALTY",
    # Media
    "ZEEL": "^CNXMEDIA", "SUNTV": "^CNXMEDIA", "PVRINOX": "^CNXMEDIA",
    # Cement
    "ULTRACEMCO": "^NSEI", "AMBUJACEM": "^NSEI", "ACC": "^NSEI",
    "SHREECEM": "^NSEI", "DALBHARAT": "^NSEI",
}

# Sectoral mapping fallback: broad NIFTY 50 index
DEFAULT_SECTOR = "^NSEI"

# Cache for sector index data (ttl 1h — sector trend doesn't flip mid-day)
_SECTOR_CACHE: Dict[str, Tuple[float, pd.DataFrame]] = {}
_CACHE_TTL_SEC = 3600


def get_sector_for(symbol: str) -> str:
    """Return sector index ticker for a symbol, or DEFAULT_SECTOR if unmapped."""
    return SYMBOL_TO_SECTOR.get(symbol.upper(), DEFAULT_SECTOR)


def _normalise_cols(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.columns = [str(c).lower() for c in df.columns]
    return df


def _fetch_sector_daily(sector_ticker: str, days: int = 60) -> Optional[pd.DataFrame]:
    """Fetch sector index daily bars, with 1h cache.

    Tries Dhan first, then falls back to yfinance. The fallback is not
    optional decoration: sector tickers here are yfinance symbols (^NSEBANK,
    ^CNXIT, ...) and the Dhan Data API subscription has expired, so the Dhan
    path returns 401 for every index. Without the fallback sector_bias()
    answers "unknown" for every symbol, which makes check_sector_alignment()
    fail open and pass 100% of trades — a gate that reads as active in code
    while being inert in production.
    """
    now = time.time()
    cached = _SECTOR_CACHE.get(sector_ticker)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    # ── Primary: Dhan ────────────────────────────────────────────────────
    try:
        from core.api_dhan import dhan_daily
        df = dhan_daily(sector_ticker, days_back=days)
        if df is not None and not df.empty:
            df = _normalise_cols(df)
            _SECTOR_CACHE[sector_ticker] = (now, df)
            return df
        log.debug(f"[Sector] {sector_ticker}: Dhan empty, trying yfinance")
    except Exception as e:
        log.debug(f"[Sector] {sector_ticker} Dhan failed ({e}), trying yfinance")

    # ── Fallback: yfinance ───────────────────────────────────────────────
    try:
        import yfinance as yf
        raw = yf.Ticker(sector_ticker).history(period=f"{days}d", interval="1d")
        if raw is None or raw.empty:
            log.debug(f"[Sector] {sector_ticker}: yfinance empty")
            return None
        df = _normalise_cols(raw.copy())
        if "close" not in df.columns:
            return None
        df = df.dropna(subset=["close"])
        if df.empty:
            return None
        _SECTOR_CACHE[sector_ticker] = (now, df)
        return df
    except Exception as e:
        log.debug(f"[Sector] {sector_ticker} yfinance failed: {e}")
        return None


def sector_bias(symbol: str) -> Tuple[str, Dict]:
    """
    Compute sector bias for a symbol.

    Returns ("bullish" | "bearish" | "neutral" | "unknown", info dict).
    """
    sector = get_sector_for(symbol)
    df = _fetch_sector_daily(sector)
    if df is None or df.empty or len(df) < 25:
        return "unknown", {"sector": sector, "reason": "no_data"}

    close = df['close']
    ema20 = close.ewm(span=20, adjust=False).mean()
    last_close = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    slope_5d = float(ema20.iloc[-1] - ema20.iloc[-5])

    info = {
        "sector": sector,
        "close": round(last_close, 2),
        "ema20": round(e20, 2),
        "slope_5d": round(slope_5d, 3),
    }

    # Bullish: close above EMA20 AND positive slope
    if last_close > e20 and slope_5d > 0:
        return "bullish", info
    # Bearish: close below EMA20 AND negative slope
    if last_close < e20 and slope_5d < 0:
        return "bearish", info
    return "neutral", info


def check_sector_alignment(symbol: str, direction: str) -> Tuple[bool, Dict]:
    """
    Gate function: block trades that fight their sector.

    Long signal: bullish or neutral sector OK; bearish sector → BLOCK
    Short signal: bearish or neutral sector OK; bullish sector → BLOCK
    Unknown sector data: pass through (degraded mode, log only)
    """
    bias, info = sector_bias(symbol)
    info["bias"] = bias
    info["direction"] = direction

    if bias == "unknown":
        # Don't block on missing data; downstream RS vs NIFTY catches most issues
        return True, {**info, "reason": "sector_data_unavailable_pass"}

    if direction == "long":
        if bias == "bearish":
            return False, {**info, "reason": "long_vs_bearish_sector"}
        return True, info
    else:  # short
        if bias == "bullish":
            return False, {**info, "reason": "short_vs_bullish_sector"}
        return True, info


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    for sym in ["HDFCBANK", "INFY", "RELIANCE", "TATAMOTORS", "DIVISLAB", "UNKNOWN_SYM"]:
        print(f"\n{sym}:")
        for d in ("long", "short"):
            ok, info = check_sector_alignment(sym, d)
            print(f"  {d}: ok={ok}  {info}")
