"""
ohlcv_fallback.py — yfinance OHLCV in the exact shape Dhan returns.

WHY
---
The intraday scanner (scan_only_v2 -> timeframe_sync) fetches every symbol's
bars through DhanAPI. The Dhan Data API subscription is expired, so every fetch
returns HTTP 401 -> empty DataFrame. Empty data means the signal engine sees
nothing, so the scanner finds ZERO trades for every symbol — not because no
setup exists, but because it never receives a single bar. This module is the
missing fallback: when Dhan comes back empty, fetch the same bars from yfinance.

SHAPE CONTRACT
--------------
Returns a DataFrame with columns exactly: date, open, high, low, close, volume
(date is a tz-naive datetime column, not the index) — identical to
core.api_dhan._parse_ohlcv, so callers need no other change.

LIMITS (honest)
---------------
  - yfinance 5m history caps at ~60 days; 15m built by resampling 5m.
  - bars are ~3 minutes behind live, not tick. Fine for a scanner that already
    throttled 0.35s/symbol; useless for anything latency-sensitive.
  - daily uses yfinance daily, back years.

This module restores DATA FLOW. It does not change what the signals are worth:
the scanner's own journal measured these signals at profit factor 0.44
(net-negative). Finding trades again means finding those same trades.
"""
from __future__ import annotations

import ssl
import warnings
from typing import Optional

import pandas as pd

warnings.filterwarnings("ignore")
ssl._create_default_https_context = ssl._create_unverified_context

_COLS = ["date", "open", "high", "low", "close", "volume"]


def _yf_ticker(symbol: str) -> str:
    """NSE symbol -> yfinance ticker, honouring the corporate-action map."""
    try:
        from core.intraday_capture import yf_ticker
        return yf_ticker(symbol)
    except Exception:
        return f"{symbol.upper()}.NS"


def _to_dhan_shape(df: pd.DataFrame) -> pd.DataFrame:
    """Rename/reset a yfinance frame to the Dhan column contract."""
    if df is None or df.empty:
        return pd.DataFrame(columns=_COLS)
    out = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                             "Close": "close", "Volume": "volume"})
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in out.columns]
    out = out[keep].copy()
    idx = pd.to_datetime(out.index)
    try:
        idx = idx.tz_localize(None)
    except (TypeError, AttributeError):
        try:
            idx = idx.tz_convert(None)
        except Exception:
            pass
    out.insert(0, "date", idx)
    out = out.reset_index(drop=True).dropna(subset=["close"])
    return out[_COLS] if not out.empty else pd.DataFrame(columns=_COLS)


def daily(symbol: str, days_back: int = 60) -> pd.DataFrame:
    """Daily OHLCV, Dhan-shaped. Empty frame on failure (never raises)."""
    try:
        import yfinance as yf
        period = f"{max(days_back + 15, 60)}d"
        raw = yf.Ticker(_yf_ticker(symbol)).history(period=period, interval="1d")
        out = _to_dhan_shape(raw)
        return out.tail(days_back) if days_back else out
    except Exception:
        return pd.DataFrame(columns=_COLS)


def intraday(symbol: str, interval: int = 5, days_back: int = 5) -> pd.DataFrame:
    """Intraday OHLCV (5m or 15m), Dhan-shaped. Empty frame on failure.

    yfinance serves 5m directly; 15m is resampled from 5m so both intervals
    come from one code path and one 60-day cap.
    """
    try:
        import yfinance as yf
        days = min(max(days_back, 1), 60)          # yfinance 5m hard cap
        raw = yf.Ticker(_yf_ticker(symbol)).history(period=f"{days}d", interval="5m")
        base = _to_dhan_shape(raw)
        if base.empty or interval <= 5:
            return base
        # Resample 5m -> requested interval (e.g. 15m).
        g = base.set_index("date")
        rule = f"{interval}min"
        agg = g.resample(rule, label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"}).dropna(subset=["close"])
        agg = agg.reset_index()
        return agg[_COLS]
    except Exception:
        return pd.DataFrame(columns=_COLS)
