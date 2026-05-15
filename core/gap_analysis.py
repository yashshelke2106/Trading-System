"""
Gap analysis — predicts direction from open vs prev close.

Gap up = today_open > yesterday_close (bullish bias)
Gap down = today_open < yesterday_close (bearish bias)

Gap types:
  - Common gap (<0.5%): meaningless, usually fades
  - Breakaway gap (0.5-2%): start of new trend
  - Runaway gap (2-5%): mid-trend continuation, strongest follow-through
  - Exhaustion gap (>5%): end of trend, often reverses

Behavior post-gap:
  - Gap fill: price moves back to close gap (mean reversion)
  - Gap and go: price never returns, momentum continues (trend follow)

Decision: combine gap type + first 15 min direction.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class GapInfo:
    symbol: str
    prev_close: float
    today_open: float
    gap_pct: float           # +ve up, -ve down
    gap_type: str            # 'common' | 'breakaway' | 'runaway' | 'exhaustion'
    bias: str                # 'bullish' | 'bearish' | 'neutral'
    likely_action: str       # 'gap_fill' | 'gap_and_go' | 'wait'
    confidence: float


def analyze_gap(symbol: str, df_5m: pd.DataFrame,
                df_daily: pd.DataFrame) -> Optional[GapInfo]:
    """Compute gap info for a symbol. Returns None if data insufficient."""
    if df_5m is None or df_5m.empty or df_daily is None or df_daily.empty:
        return None
    if len(df_daily) < 2:
        return None

    try:
        # Today's open = first 5m bar open
        df = df_5m.copy()
        df['_date'] = pd.to_datetime(df['date']).dt.date
        today = datetime.now().date()
        today_bars = df[df['_date'] == today]
        if today_bars.empty:
            today_bars = df.tail(1)
        today_open = float(today_bars['open'].iloc[0])

        # Yesterday's close
        prev_close = float(df_daily['close'].iloc[-2])
        gap_pct = (today_open - prev_close) / prev_close * 100

        # Gap classification
        abs_gap = abs(gap_pct)
        if abs_gap < 0.5:
            gap_type = 'common'
        elif abs_gap < 2.0:
            gap_type = 'breakaway'
        elif abs_gap < 5.0:
            gap_type = 'runaway'
        else:
            gap_type = 'exhaustion'

        # Bias from gap direction + type
        if gap_pct > 0:
            bias = 'bullish' if gap_type in ('breakaway', 'runaway') else 'neutral'
        elif gap_pct < 0:
            bias = 'bearish' if gap_type in ('breakaway', 'runaway') else 'neutral'
        else:
            bias = 'neutral'

        # Likely action
        if gap_type == 'common':
            likely = 'gap_fill'
        elif gap_type == 'exhaustion':
            likely = 'gap_fill'   # exhaustion gaps often reverse
        elif gap_type in ('breakaway', 'runaway'):
            # Check if first 5m bar confirms gap direction
            try:
                first_bar = today_bars.iloc[0]
                bar_close = float(first_bar['close'])
                bar_open = float(first_bar['open'])
                if (gap_pct > 0 and bar_close > bar_open) or \
                   (gap_pct < 0 and bar_close < bar_open):
                    likely = 'gap_and_go'   # confirmed momentum
                else:
                    likely = 'wait'         # first bar weak — wait for confirmation
            except Exception:
                likely = 'wait'
        else:
            likely = 'wait'

        # Confidence: gap size + classification
        if gap_type == 'runaway':
            conf = 0.75
        elif gap_type == 'breakaway':
            conf = 0.65
        elif gap_type == 'exhaustion':
            conf = 0.55  # reverse trade is risky
        else:
            conf = 0.35

        return GapInfo(
            symbol=symbol,
            prev_close=prev_close,
            today_open=today_open,
            gap_pct=round(gap_pct, 2),
            gap_type=gap_type,
            bias=bias,
            likely_action=likely,
            confidence=conf,
        )
    except Exception as e:
        log.debug(f"gap analysis failed for {symbol}: {e}")
        return None
