"""
Directional Predictor — multi-factor model for next 30-60 min direction.

Combines 8 indicators into a single up/down probability score:

  1. Gap (yesterday close → today open):         +/- 15 points
  2. ORB position (above/below opening range):   +/- 12 points
  3. VWAP relationship (above/below):             +/- 10 points
  4. EMA stack (9>21>50):                         +/- 10 points
  5. RSI momentum (>60 bull, <40 bear):           +/-  8 points
  6. Volume confirmation (rising vol w/ trend):   +/-  8 points
  7. Higher highs / lower lows (last 5 bars):     +/-  8 points
  8. Macro bias (NIFTY direction):                +/-  6 points

Score range: -77 (max bear) to +77 (max bull)
  >  +40 = STRONG_UP
  +20-40 = MODERATE_UP
  -20-+20 = NEUTRAL
  -40 to -20 = MODERATE_DOWN
  <  -40 = STRONG_DOWN

Probability = sigmoid(score / 30) → maps to 0..1 win prob.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class DirectionPrediction:
    symbol: str
    direction: str               # 'up' | 'down' | 'neutral'
    classification: str          # 'STRONG_UP' | 'MODERATE_UP' | ...
    score: int                   # -77 to +77
    probability_up: float        # 0..1
    factors: Dict[str, int] = field(default_factory=dict)   # individual scores


def predict_direction(symbol: str,
                       df_5m: pd.DataFrame,
                       df_15m: Optional[pd.DataFrame] = None,
                       df_daily: Optional[pd.DataFrame] = None,
                       nifty_pct: float = 0.0) -> Optional[DirectionPrediction]:
    """Compute directional bias score and probability."""
    if df_5m is None or df_5m.empty or len(df_5m) < 10:
        return None

    factors = {}
    score = 0

    try:
        close = float(df_5m['close'].iloc[-1])

        # ── 1. Gap analysis ─────────────────────────────────────────────
        try:
            from core.gap_analysis import analyze_gap
            if df_daily is not None and len(df_daily) >= 2:
                gap = analyze_gap(symbol, df_5m, df_daily)
                if gap:
                    if gap.gap_type == 'runaway':
                        s = 15 if gap.gap_pct > 0 else -15
                    elif gap.gap_type == 'breakaway':
                        s = 10 if gap.gap_pct > 0 else -10
                    elif gap.gap_type == 'exhaustion':
                        s = -8 if gap.gap_pct > 0 else 8   # reverse
                    else:
                        s = 0
                    factors['gap'] = s
                    score += s
        except Exception:
            pass

        # ── 2. ORB position ─────────────────────────────────────────────
        try:
            from core.opening_range import get_orb_levels
            orb = get_orb_levels(df_5m)
            if orb:
                orb_high, orb_low, _ = orb
                if close > orb_high:
                    factors['orb'] = 12
                    score += 12
                elif close < orb_low:
                    factors['orb'] = -12
                    score -= 12
                elif close > (orb_high + orb_low) / 2:
                    factors['orb'] = 4
                    score += 4
                else:
                    factors['orb'] = -4
                    score -= 4
        except Exception:
            pass

        # ── 3. VWAP relationship ────────────────────────────────────────
        try:
            tp = (df_5m['high'] + df_5m['low'] + df_5m['close']) / 3
            vwap = (tp * df_5m['volume']).cumsum() / df_5m['volume'].cumsum()
            vwap_val = float(vwap.iloc[-1])
            if vwap_val > 0:
                pct_from_vwap = (close - vwap_val) / vwap_val * 100
                if pct_from_vwap > 0.3:
                    factors['vwap'] = 10
                    score += 10
                elif pct_from_vwap < -0.3:
                    factors['vwap'] = -10
                    score -= 10
                else:
                    factors['vwap'] = int(pct_from_vwap * 20)
                    score += factors['vwap']
        except Exception:
            pass

        # ── 4. EMA stack ────────────────────────────────────────────────
        try:
            e9 = df_5m['close'].ewm(span=9).mean().iloc[-1]
            e21 = df_5m['close'].ewm(span=21).mean().iloc[-1]
            e50 = df_5m['close'].ewm(span=50).mean().iloc[-1] if len(df_5m) >= 50 else e21

            if e9 > e21 > e50:
                factors['ema_stack'] = 10
                score += 10
            elif e9 < e21 < e50:
                factors['ema_stack'] = -10
                score -= 10
            elif e9 > e21:
                factors['ema_stack'] = 5
                score += 5
            elif e9 < e21:
                factors['ema_stack'] = -5
                score -= 5
        except Exception:
            pass

        # ── 5. RSI momentum ─────────────────────────────────────────────
        try:
            delta = df_5m['close'].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rs))
            rsi_val = float(rsi.iloc[-1])
            if rsi_val > 70:
                factors['rsi'] = 8
                score += 8
            elif rsi_val > 55:
                factors['rsi'] = 5
                score += 5
            elif rsi_val < 30:
                factors['rsi'] = -8
                score -= 8
            elif rsi_val < 45:
                factors['rsi'] = -5
                score -= 5
        except Exception:
            pass

        # ── 6. Volume confirmation ──────────────────────────────────────
        try:
            v5 = float(df_5m['volume'].iloc[-1])
            v_avg = float(df_5m['volume'].rolling(20).mean().iloc[-1])
            vol_ratio = v5 / v_avg if v_avg > 0 else 1.0

            # Direction of last bar
            last_close = float(df_5m['close'].iloc[-1])
            prev_close = float(df_5m['close'].iloc[-2]) if len(df_5m) >= 2 else last_close
            bar_dir = 1 if last_close > prev_close else (-1 if last_close < prev_close else 0)

            if vol_ratio >= 1.5:
                s = 8 * bar_dir
                factors['volume'] = s
                score += s
            elif vol_ratio >= 1.2:
                s = 5 * bar_dir
                factors['volume'] = s
                score += s
        except Exception:
            pass

        # ── 7. Higher highs / lower lows (last 5 bars) ──────────────────
        try:
            highs = df_5m['high'].iloc[-5:].tolist()
            lows = df_5m['low'].iloc[-5:].tolist()
            hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
            ll = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i-1])

            if hh >= 3:
                factors['structure'] = 8
                score += 8
            elif ll >= 3:
                factors['structure'] = -8
                score -= 8
            elif hh > ll:
                factors['structure'] = 4
                score += 4
            elif ll > hh:
                factors['structure'] = -4
                score -= 4
        except Exception:
            pass

        # ── 8. Macro bias (NIFTY direction) ─────────────────────────────
        if nifty_pct > 0.3:
            factors['macro'] = 6
            score += 6
        elif nifty_pct < -0.3:
            factors['macro'] = -6
            score -= 6
        elif nifty_pct > 0:
            factors['macro'] = 2
            score += 2
        elif nifty_pct < 0:
            factors['macro'] = -2
            score -= 2

        # ── Classification ──────────────────────────────────────────────
        if score >= 40:
            classification = 'STRONG_UP'
            direction = 'up'
        elif score >= 20:
            classification = 'MODERATE_UP'
            direction = 'up'
        elif score <= -40:
            classification = 'STRONG_DOWN'
            direction = 'down'
        elif score <= -20:
            classification = 'MODERATE_DOWN'
            direction = 'down'
        else:
            classification = 'NEUTRAL'
            direction = 'neutral'

        # Sigmoid for probability
        prob_up = 1 / (1 + math.exp(-score / 30))

        return DirectionPrediction(
            symbol=symbol,
            direction=direction,
            classification=classification,
            score=score,
            probability_up=round(prob_up, 3),
            factors=factors,
        )
    except Exception as e:
        log.debug(f"predict_direction failed for {symbol}: {e}")
        return None
