"""
Opening Range Breakout (ORB) — classic intraday momentum strategy.

Concept: First 15 min (9:15-9:30) sets the "opening range". Price breaking
above ORB high = bullish continuation. Below ORB low = bearish breakdown.

ORB high/low = anchor points. Strongest moves occur when ORB break has:
  - Volume confirmation (>1.5x avg 5m vol)
  - Wide ORB (>0.5% width = real conviction, not a doji)
  - Break occurs 9:30-10:30 (not later — late breaks fail more)

Output: signals that bypass min_votes/min_strength because the ORB break
itself IS the signal.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# Opening range definition
ORB_START = dtime(9, 15)
ORB_END   = dtime(9, 30)
# After ORB completes, watch for break until this time
ORB_BREAK_DEADLINE = dtime(10, 30)
# Minimum range width as % of price (avoids breakouts of tiny doji ranges)
MIN_RANGE_PCT = 0.005   # 0.5%
# Volume confirmation on break
BREAK_VOL_MULT = 1.5


@dataclass
class ORBSignal:
    symbol: str
    direction: str        # 'long' or 'short'
    orb_high: float
    orb_low: float
    orb_width_pct: float
    entry_price: float
    stop_loss: float      # opposite side of ORB
    target_1: float       # 1R
    target_2: float       # 2R
    break_vol_ratio: float
    confidence: float     # 0..1


def get_orb_levels(df_5m: pd.DataFrame) -> Optional[tuple]:
    """
    Extract first 3 bars (9:15-9:30 = 15min ORB on 5min chart).
    Returns (orb_high, orb_low, orb_open) or None if not enough data yet.
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 3:
        return None

    try:
        df = df_5m.copy()
        df['_dt'] = pd.to_datetime(df['date'])
        df['_date'] = df['_dt'].dt.date
        df['_time'] = df['_dt'].dt.time

        today = datetime.now().date()
        today_bars = df[df['_date'] == today]
        if today_bars.empty:
            today_bars = df.tail(80)  # fallback to recent bars

        orb_bars = today_bars[(today_bars['_time'] >= ORB_START)
                              & (today_bars['_time'] < ORB_END)]

        if len(orb_bars) < 2:
            return None

        orb_high = float(orb_bars['high'].max())
        orb_low = float(orb_bars['low'].min())
        orb_open = float(orb_bars['open'].iloc[0])
        return (orb_high, orb_low, orb_open)
    except Exception as e:
        log.debug(f"ORB levels failed: {e}")
        return None


def detect_orb_break(symbol: str, df_5m: pd.DataFrame) -> Optional[ORBSignal]:
    """
    Detect a confirmed Opening Range Breakout.

    Logic:
      1. ORB must have completed (current time > 9:30)
      2. Current time before 10:30 deadline
      3. Latest 5m bar closed above ORB high (long) OR below ORB low (short)
      4. Break bar volume > 1.5x avg of prior bars
      5. ORB width >= 0.5% (avoid tight-range fakeouts)
    """
    now = datetime.now().time()
    if now < ORB_END or now > ORB_BREAK_DEADLINE:
        return None

    levels = get_orb_levels(df_5m)
    if levels is None:
        return None

    orb_high, orb_low, orb_open = levels
    if orb_high <= orb_low:
        return None

    range_pct = (orb_high - orb_low) / orb_open
    if range_pct < MIN_RANGE_PCT:
        return None

    # Last completed 5m bar
    try:
        last = df_5m.iloc[-1]
        close = float(last['close'])
        vol = float(last['volume'])

        # Average volume of prior 5 bars
        prior_vols = df_5m['volume'].iloc[-6:-1]
        avg_vol = float(prior_vols.mean()) if len(prior_vols) > 0 else vol
        vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0

        if vol_ratio < BREAK_VOL_MULT:
            return None

        if close > orb_high:
            # Long breakout
            stop = orb_low
            risk = close - stop
            return ORBSignal(
                symbol=symbol, direction='long',
                orb_high=orb_high, orb_low=orb_low,
                orb_width_pct=range_pct * 100,
                entry_price=close, stop_loss=stop,
                target_1=close + risk,
                target_2=close + risk * 2,
                break_vol_ratio=vol_ratio,
                confidence=min(0.95, 0.6 + (vol_ratio - 1.5) * 0.1 + range_pct * 30),
            )
        elif close < orb_low:
            stop = orb_high
            risk = stop - close
            return ORBSignal(
                symbol=symbol, direction='short',
                orb_high=orb_high, orb_low=orb_low,
                orb_width_pct=range_pct * 100,
                entry_price=close, stop_loss=stop,
                target_1=close - risk,
                target_2=close - risk * 2,
                break_vol_ratio=vol_ratio,
                confidence=min(0.95, 0.6 + (vol_ratio - 1.5) * 0.1 + range_pct * 30),
            )

        return None
    except Exception as e:
        log.debug(f"ORB break detect failed for {symbol}: {e}")
        return None
