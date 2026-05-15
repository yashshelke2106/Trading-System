"""
Top Mover Capture Mode.

When stock is clearly today's top gainer/loser (% change + volume confirmed),
bypass defensive filters that block momentum trades.

A stock at +5% on 5x volume is NOT noise — it's the day's signal. Treating it
like a normal trade (and blocking it for high vol/RSI) misses the obvious play.

Triggers when:
  - intraday_change_pct >= TOP_MOVER_PCT (3%) AND
  - vol_ratio >= TOP_MOVER_VOL (2.0x)

Bypasses:
  - max_volume_ratio cap (movers HAVE high vol)
  - disable_shorts (for top losers in confirmed downtrend)
  - C-tier hard reject (these are often the top movers)
  - block_after_hour partial (lets 13:00-14:30 movers through)
  - chop window (lets 9:15-9:20 gap-ups through)

Still enforces:
  - min_votes (need pattern confluence)
  - 15m direction alignment (preferred)
  - Risk per trade caps
"""

import logging
from typing import Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

TOP_MOVER_PCT = 3.0       # +3% or -3% intraday = top mover
TOP_MOVER_VOL = 2.0       # 2x volume = institutional interest
EXTREME_MOVER_PCT = 5.0   # +5% / -5% = override even more gates


def detect_top_mover(df_5m: pd.DataFrame, df_1d: Optional[pd.DataFrame] = None
                     ) -> Tuple[bool, str, float, float]:
    """
    Returns (is_top_mover, classification, intraday_pct, vol_ratio).

    classification ∈ {'normal', 'top_gainer', 'top_loser',
                       'extreme_gainer', 'extreme_loser'}
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 2:
        return False, 'normal', 0.0, 0.0

    try:
        # Open price = first bar's open today (or yesterday close as fallback)
        from datetime import datetime
        today = datetime.now().date()

        try:
            df_5m['_date'] = pd.to_datetime(df_5m['date']).dt.date
            today_bars = df_5m[df_5m['_date'] == today]
            if today_bars.empty:
                today_bars = df_5m.tail(20)
            day_open = float(today_bars['open'].iloc[0])
        except Exception:
            day_open = float(df_5m['open'].iloc[0])

        current = float(df_5m['close'].iloc[-1])
        intraday_pct = (current - day_open) / day_open * 100

        # Today's cumulative volume vs 20-day daily avg
        if df_1d is not None and len(df_1d) >= 21:
            avg_daily_vol = float(df_1d['volume'].rolling(20).mean().iloc[-1])
        else:
            avg_daily_vol = float(df_5m['volume'].rolling(60).mean().iloc[-1]) * 75 / 5
            # Approx: 5m vol * 75 bars (full day) / current bars

        today_vol = float(df_5m['volume'].sum()) if not df_5m.empty else 0
        vol_ratio = today_vol / avg_daily_vol if avg_daily_vol > 0 else 1.0

        # Classification
        abs_pct = abs(intraday_pct)
        if abs_pct >= EXTREME_MOVER_PCT and vol_ratio >= TOP_MOVER_VOL:
            cls = 'extreme_gainer' if intraday_pct > 0 else 'extreme_loser'
            return True, cls, intraday_pct, vol_ratio
        elif abs_pct >= TOP_MOVER_PCT and vol_ratio >= TOP_MOVER_VOL:
            cls = 'top_gainer' if intraday_pct > 0 else 'top_loser'
            return True, cls, intraday_pct, vol_ratio

        return False, 'normal', intraday_pct, vol_ratio
    except Exception as e:
        log.debug(f"detect_top_mover failed: {e}")
        return False, 'normal', 0.0, 0.0


def get_override_config(classification: str) -> dict:
    """
    Returns config overrides for top movers.

    Empty dict = no override (normal stock).
    Selective relaxation of filters based on mover intensity.
    """
    if classification == 'normal':
        return {}

    base = {
        # Top movers HAVE high volume — that's why they're moving
        "max_volume_ratio": 10.0,
        # Pattern confluence still required but slightly relaxed
        "min_votes": 3,
        "min_vote_lead": 1,
        # Lower strength bar — patterns lag parabolic moves
        "min_strength": 35,
        # Top movers GET wider targets to capture the day's move (50% premium goal)
        "rr_ratio": 4.5,
        "max_sl_pct": 0.035,
    }

    if classification in ('top_loser', 'extreme_loser'):
        # Confirmed downtrend stock — shorts are the play. Mirror gainer's RSI bar.
        base["disable_shorts"] = False
        base["rsi_short_floor"] = 25  # symmetric mirror of gainer rsi_long_momentum_min=50 → 100-50=50? we use 25 = mirror of 75
        # Both directions need momentum continuation — short can chase deep oversold here

    if classification in ('top_gainer', 'extreme_gainer'):
        # Riding momentum — RSI may already be high
        base["rsi_long_momentum_min"] = 50

    if classification.startswith('extreme'):
        # Parabolic moves — even more aggressive bypass + bigger target
        base["min_votes"] = 2
        base["min_strength"] = 25
        base["rr_ratio"] = 5.0
        base["max_sl_pct"] = 0.04

    return base


def should_bypass_chop_window(classification: str) -> bool:
    """Top movers at the open? Don't skip them — that's where the alpha is."""
    return classification != 'normal'


def should_bypass_c_tier(classification: str) -> bool:
    """C-tier stocks that are top movers today are NOT today's losers — chase them."""
    return classification != 'normal'


def should_bypass_afternoon_block(classification: str, hour: int) -> bool:
    """Extreme movers in 13:00-14:30 still tradeable. After 14:30 still block."""
    if hour >= 15:
        return False  # last 30 min — too risky
    if classification.startswith('extreme'):
        return True
    if classification in ('top_gainer', 'top_loser') and hour <= 14:
        return True
    return False


def should_bypass_symbol_blacklist(classification: str) -> bool:
    """Yesterday's loser is today's mover. Override symbol blacklist for top movers."""
    return classification.startswith('extreme')
