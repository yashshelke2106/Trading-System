"""
Intraday high-confidence volume spike detector.
Input: 5-min OHLCV bars for today's session.
Output: SpikeAlert with confidence score, direction, and reason.

Confidence scoring (max 100):
  Volume surge  : up to 40 pts  (2.5x=16, 3.5x=24, 5x=32, 8x=40)
  Directional   : up to 50 pts  (bullish candle=15, VWAP alignment=15, session breakout=20)
  Consensus     : +5  (all 3 directional factors agree)
  Prime window  : +10 (09:15-10:30 or 13:30-15:15)
  Price move    : +5  (candle body > 0.5%)
Emits alert only when confidence >= 55 AND vol_ratio >= 2.5x.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class SpikeAlert:
    symbol: str
    direction: str          # "long" or "short"
    confidence: int         # 0-100
    vol_ratio: float        # current_bar_vol / trailing_20bar_avg
    current_price: float
    vwap: float
    price_move_pct: float   # % body size in trigger candle
    is_breakout: bool       # new session high (long) or low (short)
    in_prime_time: bool
    above_vwap: bool
    candle_bullish: bool
    timestamp: str
    reason: str


class VolumeSpikeDetector:
    MIN_BARS = 8
    VOL_THRESHOLD = 2.5
    CONFIDENCE_THRESHOLD = 55

    def detect_from_df(self, df: pd.DataFrame, symbol: str) -> Optional[SpikeAlert]:
        if df is None or len(df) < self.MIN_BARS:
            return None

        vols   = df["volume"].values.astype(float)
        closes = df["close"].values.astype(float)
        opens  = df["open"].values.astype(float)
        highs  = df["high"].values.astype(float)
        lows   = df["low"].values.astype(float)

        last_vol = vols[-1]
        lookback = min(20, len(vols) - 1)
        if lookback < 3:
            return None
        trailing_avg = np.mean(vols[-lookback - 1:-1])
        if trailing_avg <= 0:
            return None

        vol_ratio = last_vol / trailing_avg
        if vol_ratio < self.VOL_THRESHOLD:
            return None

        # VWAP: cumulative sum(close*vol)/sum(vol)
        vwap = float(np.dot(closes, vols) / vols.sum()) if vols.sum() > 0 else closes[-1]

        last_close = closes[-1]
        last_open  = opens[-1]
        last_high  = highs[-1]
        last_low   = lows[-1]

        candle_bullish = last_close > last_open
        above_vwap     = last_close > vwap

        # Session breakout: new high/low vs recent N bars
        n = min(20, len(highs))
        is_new_high = last_high >= highs[-n:].max() * 0.999
        is_new_low  = last_low  <= lows[-n:].min()  * 1.001

        # Prime time window (IST minutes since midnight)
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        in_prime = (9 * 60 + 15 <= now_min <= 10 * 60 + 30) or \
                   (13 * 60 + 30 <= now_min <= 15 * 60 + 15)

        long_votes  = int(candle_bullish) + int(above_vwap) + int(is_new_high)
        short_votes = int(not candle_bullish) + int(not above_vwap) + int(is_new_low)

        if long_votes == short_votes:
            return None  # ambiguous

        direction = "long" if long_votes > short_votes else "short"

        # ── Confidence scoring ──────────────────────────────────────────────
        score = 0

        if   vol_ratio >= 8.0: score += 40
        elif vol_ratio >= 5.0: score += 32
        elif vol_ratio >= 3.5: score += 24
        else:                  score += 16

        if direction == "long":
            if candle_bullish: score += 15
            if above_vwap:     score += 15
            if is_new_high:    score += 20
        else:
            if not candle_bullish: score += 15
            if not above_vwap:     score += 15
            if is_new_low:         score += 20

        if long_votes == 3 or short_votes == 3:
            score += 5
        if in_prime:
            score += 10

        price_move_pct = abs(last_close - last_open) / last_open * 100 if last_open > 0 else 0
        if price_move_pct > 0.5:
            score += 5

        if score < self.CONFIDENCE_THRESHOLD:
            return None

        # ── Build reason ────────────────────────────────────────────────────
        parts = []
        parts.append(f"Vol {vol_ratio:.1f}x {'surge' if vol_ratio >= 5 else 'spike'}")
        if direction == "long":
            if is_new_high:    parts.append("Session high break")
            if above_vwap:     parts.append("Above VWAP")
            if candle_bullish: parts.append("Bullish candle")
        else:
            if is_new_low:         parts.append("Session low break")
            if not above_vwap:     parts.append("Below VWAP")
            if not candle_bullish: parts.append("Bearish candle")
        if in_prime:
            parts.append("Prime window")

        return SpikeAlert(
            symbol=symbol,
            direction=direction,
            confidence=min(score, 100),
            vol_ratio=round(vol_ratio, 2),
            current_price=round(last_close, 2),
            vwap=round(vwap, 2),
            price_move_pct=round(price_move_pct, 3),
            is_breakout=is_new_high if direction == "long" else is_new_low,
            in_prime_time=in_prime,
            above_vwap=above_vwap,
            candle_bullish=candle_bullish,
            timestamp=now.isoformat(),
            reason=" · ".join(parts),
        )
