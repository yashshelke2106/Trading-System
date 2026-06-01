"""
India Swing Strategy — elite swing-trader rewrite of entry logic.

Replaces 17-detector vote-stack (signal_engine.generate_signal) with FIVE
sequential, binary gates. No voting. No partial credit. All must pass.

Premise (from 932-trade journal audit, 2026-05-26):
  - 17-detector vote stack delivers 30.5% WR, Grade-A no edge vs Grade-C.
  - Most votes correlate (EMA cross + EMA stack + supertrend + range filter
    all measure trend direction). Stacking adds no information.
  - Indian F&O stocks need: trend + pullback + confirmation + structural stop
    + sector/RS alignment. 1:3 RR mandatory.

Gates (executed in order, short-circuit on first fail):
  G1  HTF Trend       — Daily EMA20 > EMA50 stack + slope (or inverse for short)
  G2  Pullback / Base — last 1-5 bars touched EMA20 or compressed (body ≤ 0.7×ATR)
  G3  Confirmation    — engulfing / marubozu / pin-bar / 5-bar breakout-close
                        AND volume ≥ 1.3× 20-bar avg
  G4  Structural Risk — SL = swing low/high (5-bar pivot, 20-bar lookback);
                        Target = entry ± 3R. Reject if no valid swing.
  G5  Quality Filters — RSI in healthy zone, RS vs NIFTY ≥ 1.05 (long) or
                        ≤ 0.95 (short), 52-week proximity bonus.

Output: IndiaSwingSignal — same downstream contract as legacy Signal
(symbol, direction, entry_price, sl_price, target_price, patterns,
 confluence_grade, confluence_score, reason, ts).

NOT included in this gate (deliberately):
  - Liquidity sweep / stop hunt: intraday-only concept, doesn't apply
    cleanly to daily-bar swing entries.
  - VWAP: resets daily, useless for multi-day holds.
  - CPR breakout: index-intraday tool.
  - Time-of-day windows: swing decisions are made on the close.
  - Sector index gate: NIFTY-only RS used as proxy (sector indices need
    extra data plumbing — left as TODO for sector_rotation.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Config — tuned for daily-bar F&O stocks
# ──────────────────────────────────────────────────────────────────────────

EMA_FAST              = 20
EMA_SLOW              = 50
RSI_LEN               = 14
ATR_LEN               = 14

# v3 PRECISION MODE — data-driven tightening from v1 backtest analysis
# (16W/113L = 12.4% WR on 230 trades).
#
# Hard findings from v1 CSV:
#   - breakout_5d_high/low: 9-11% WR (n=101) → noise generator, BLOCKED
#   - RSI 65-75 long zone:  8.2% WR (n=90)  → chasing, BLOCKED
#   - RSI 55-65 long zone:  25.0% WR        → keep
#   - Near 52WH:            anti-predictive  → flip to PENALTY
#   - Winners avg hold:     10.3 bars       → extend horizon
#   - Losers die:           5.3 bars        → stops too tight or chase entries
#
# Set PRECISION_MODE=False env to revert to permissive v2 thresholds.
import os
PRECISION_MODE = os.environ.get("PRECISION_MODE", "1") != "0"

if PRECISION_MODE:
    # v4 factor analysis (230-trade 2yr backtest + 919-trade journal, both
    # judged on spot, within-direction): long win-rate by RSI is 55% at 50-60
    # but collapses to 38-40% at 60-70+. Mechanism: don't buy overbought —
    # mid-RSI mean-reversion entries win, chasing strength loses. Tightened the
    # long ceiling 65 -> 60. (FINDINGS_FACTORS.md)
    RSI_LONG_MIN, RSI_LONG_MAX   = 50.0, 60.0   # v4: 60-70 zone is 40% WR — cut
    RSI_SHORT_MIN, RSI_SHORT_MAX = 25.0, 40.0   # v2 already tight, keep
    VOL_MIN_X                    = 2.0          # v3: was 1.3, raise bar
    MIN_RR                       = 1.5
    RS_LONG_MIN_VS_NIFTY         = 1.05         # v3: tighter than v2's 1.03
    RS_SHORT_MAX_VS_NIFTY        = 0.92
    RS_SHORT_RSI_MAX             = 40.0
    NEAR_52W_HIGH_PENALTY        = True         # v3: flip from bonus to penalty
    REQUIRE_REVERSAL_CANDLE      = True         # v3: block standalone breakouts
else:
    RSI_LONG_MIN, RSI_LONG_MAX   = 45.0, 75.0
    RSI_SHORT_MIN, RSI_SHORT_MAX = 25.0, 55.0
    VOL_MIN_X                    = 1.3
    MIN_RR                       = 1.5
    RS_LONG_MIN_VS_NIFTY         = 1.03
    RS_SHORT_MAX_VS_NIFTY        = 0.92
    RS_SHORT_RSI_MAX             = 40.0
    NEAR_52W_HIGH_PENALTY        = False
    REQUIRE_REVERSAL_CANDLE      = False

# Walk-forward env overrides (last word — wins over PRECISION defaults)
VOL_MIN_X    = float(os.environ.get("WF_VOL_MIN_X", VOL_MIN_X))
RSI_LONG_MAX = float(os.environ.get("WF_RSI_LONG_MAX", RSI_LONG_MAX))

SWING_PIVOT_N         = 3
SWING_LOOKBACK        = 20
PULLBACK_MAX_BARS     = 5
COMPRESS_MAX_BODY_ATR = 0.7
EXTENDED_MAX_BODY_ATR = 1.5
EMA20_PULLBACK_TOL    = 0.015
PIVOT_ATR_BUFFER      = 0.5

NEAR_52W_HIGH_PCT     = 0.90
NEAR_52W_LOW_PCT      = 1.10


# ──────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class IndiaSwingSignal:
    symbol: str
    direction: str               # "long" | "short"
    entry_price: float
    sl_price: float
    target_price: float
    rr_ratio: float
    confluence_grade: str        # S | A | B
    confluence_score: float      # 0..100
    patterns: List[str]
    patterns_combined: List[str] # alias for downstream tracker
    reason: str
    ts: str

    # Diagnostic fields (downstream may persist)
    gate_results: Dict[str, bool] = field(default_factory=dict)
    ema20: float = 0.0
    ema50: float = 0.0
    rsi: float = 0.0
    volume_ratio: float = 0.0
    atr: float = 0.0
    rs_vs_nifty: float = 0.0
    near_52wh: bool = False
    near_52wl: bool = False
    strength: float = 0.0        # legacy field — ranker uses it
    strategy: str = "india_swing"

    def to_dict(self) -> Dict:
        """Match legacy Signal dict shape so scan_only_v2 enrichment works."""
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "sl_price": self.sl_price,
            "target_price": self.target_price,
            "rr_ratio": self.rr_ratio,
            "confluence_grade": self.confluence_grade,
            "confluence_score": self.confluence_score,
            "patterns": self.patterns,
            "patterns_combined": self.patterns_combined,
            "reason": self.reason,
            "ts": self.ts,
            "rsi": self.rsi,
            "volume_ratio": self.volume_ratio,
            "ema20": self.ema20,
            "ema50": self.ema50,
            "atr": self.atr,
            "rs_vs_nifty": self.rs_vs_nifty,
            "near_52wh": self.near_52wh,
            "near_52wl": self.near_52wl,
            "strength": self.strength,
            "strategy": self.strategy,
            "gate_results": self.gate_results,
            # Compatibility with TimeframeSyncEngine schema
            "sl_tight": self.sl_price,
            "target_1": self.entry_price + 0.5 * (self.target_price - self.entry_price),
            "vote_margin": int(self.confluence_score / 10),  # cosmetic
        }


# ──────────────────────────────────────────────────────────────────────────
# Indicator helpers (lean, no external deps beyond pandas/numpy)
# ──────────────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    dn = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _swing_low(df: pd.DataFrame, n: int = 3, lookback: int = 20) -> Optional[float]:
    """Return most-recent pivot low: bar where n bars left+right are higher."""
    if len(df) < lookback + n + 1:
        return None
    lows = df['low'].iloc[-(lookback + n + 1):].values
    # walk backwards from most recent (excluding the last n bars which can't
    # be confirmed pivots yet)
    for i in range(len(lows) - n - 1, n - 1, -1):
        if all(lows[i] < lows[i - j] for j in range(1, n + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, n + 1)):
            return float(lows[i])
    return None


def _swing_high(df: pd.DataFrame, n: int = 3, lookback: int = 20) -> Optional[float]:
    if len(df) < lookback + n + 1:
        return None
    highs = df['high'].iloc[-(lookback + n + 1):].values
    for i in range(len(highs) - n - 1, n - 1, -1):
        if all(highs[i] > highs[i - j] for j in range(1, n + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, n + 1)):
            return float(highs[i])
    return None


# ──────────────────────────────────────────────────────────────────────────
# GATES — sequential, binary, short-circuit
# ──────────────────────────────────────────────────────────────────────────

def gate0_regime(nifty_df: Optional[pd.DataFrame], direction_hint: Optional[str] = None
                 ) -> Tuple[bool, Dict]:
    """
    v3 GATE 0: Market regime filter.

    Skips entries when overall NIFTY regime is hostile to the direction:
      - Bullish regime (NIFTY close > NIFTY EMA50, slope > 0): longs OK, shorts BLOCKED
      - Bearish regime: shorts OK, longs BLOCKED
      - Neutral / no data: pass through (degrade gracefully)
    """
    if nifty_df is None or nifty_df.empty or len(nifty_df) < EMA_SLOW + 5:
        return True, {"reason": "no_nifty_regime_data_pass"}

    close = nifty_df['close']
    ema50 = _ema(close, EMA_SLOW)
    last_close = float(close.iloc[-1])
    e50 = float(ema50.iloc[-1])
    slope = float(ema50.iloc[-1] - ema50.iloc[-5])

    bullish = last_close > e50 and slope > 0
    bearish = last_close < e50 and slope < 0

    info = {
        "nifty_close": round(last_close, 1),
        "nifty_ema50": round(e50, 1),
        "nifty_slope_5d": round(slope, 2),
        "regime": "bullish" if bullish else ("bearish" if bearish else "neutral"),
    }

    if direction_hint == "long" and bearish:
        return False, {**info, "reason": "long_in_bearish_regime"}
    if direction_hint == "short" and bullish:
        return False, {**info, "reason": "short_in_bullish_regime"}
    return True, info


def gate1_htf_trend(df_daily: pd.DataFrame) -> Tuple[Optional[str], Dict]:
    """
    Daily trend gate. Sets direction.
    Long  : close > EMA20 > EMA50 and EMA20 slope > 0
    Short : close < EMA20 < EMA50 and EMA20 slope < 0
    """
    if len(df_daily) < EMA_SLOW + 5:
        return None, {"reason": f"need_{EMA_SLOW + 5}_bars"}

    close = df_daily['close']
    ema20 = _ema(close, EMA_FAST)
    ema50 = _ema(close, EMA_SLOW)

    last_close = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    e20_slope = float(ema20.iloc[-1] - ema20.iloc[-5])

    info = {
        "close": round(last_close, 2),
        "ema20": round(e20, 2),
        "ema50": round(e50, 2),
        "ema20_slope_5d": round(e20_slope, 3),
    }

    if last_close > e20 > e50 and e20_slope > 0:
        return "long", info
    if last_close < e20 < e50 and e20_slope < 0:
        return "short", info
    return None, {**info, "reason": "no_trend_stack"}


def gate2_pullback(df_daily: pd.DataFrame, direction: str) -> Tuple[bool, Dict]:
    """
    Look for pullback to EMA20 OR consolidation. Reject if current candle is
    extended (chasing). Need: (touched-EMA OR compressed) AND not-extended.
    """
    if len(df_daily) < 25:
        return False, {"reason": "insufficient_history"}

    close = df_daily['close']
    ema20 = _ema(close, EMA_FAST)
    atr_series = _atr(df_daily, ATR_LEN)
    last_atr = float(atr_series.iloc[-1])
    if last_atr <= 0 or np.isnan(last_atr):
        return False, {"reason": "bad_atr"}

    # v2: Pullback + REJECTION. Bar that touched EMA20 must ALSO close back
    # in direction (long: close > EMA20). This filters out pullbacks that
    # keep falling through EMA20. v1 accepted any touch = caught knife.
    touched = False
    rejected = False
    touch_bar_idx = None
    for i in range(1, PULLBACK_MAX_BARS + 1):
        e20_i = float(ema20.iloc[-i])
        c_i = float(df_daily['close'].iloc[-i])
        if direction == "long":
            lo_i = float(df_daily['low'].iloc[-i])
            if (lo_i <= e20_i * (1 + EMA20_PULLBACK_TOL) and
                lo_i >= e20_i * (1 - EMA20_PULLBACK_TOL)):
                touched = True
                touch_bar_idx = -i
                # Rejection: that bar closed back above EMA20 (held as support)
                if c_i > e20_i:
                    rejected = True
                break
        else:
            hi_i = float(df_daily['high'].iloc[-i])
            if (hi_i >= e20_i * (1 - EMA20_PULLBACK_TOL) and
                hi_i <= e20_i * (1 + EMA20_PULLBACK_TOL)):
                touched = True
                touch_bar_idx = -i
                if c_i < e20_i:
                    rejected = True
                break

    # Compression: avg of last 3 candle bodies ≤ COMPRESS_MAX_BODY_ATR × ATR
    bodies = (df_daily['close'] - df_daily['open']).abs()
    avg_body3 = float(bodies.iloc[-3:].mean())
    compressed = avg_body3 <= COMPRESS_MAX_BODY_ATR * last_atr

    # Not-extended: current candle body ≤ EXTENDED_MAX_BODY_ATR × ATR
    current_body = float(bodies.iloc[-1])
    not_extended = current_body <= EXTENDED_MAX_BODY_ATR * last_atr

    # v2: accept if (rejection from EMA20) OR (compression base) — touch alone
    # without rejection is now insufficient.
    setup_ok = rejected or compressed
    ok = setup_ok and not_extended

    return ok, {
        "touched_ema20": touched,
        "rejected_ema20": rejected,
        "touch_bar": touch_bar_idx,
        "compressed": compressed,
        "current_body_atr": round(current_body / last_atr, 2),
        "avg_body3_atr": round(avg_body3 / last_atr, 2),
        "not_extended": not_extended,
        "reason": (
            "ok" if ok
            else ("extended_candle" if not not_extended
                  else "touched_no_rejection" if (touched and not rejected and not compressed)
                  else "no_pullback_or_compress")
        ),
    }


def gate3_confirmation(df_daily: pd.DataFrame, direction: str) -> Tuple[bool, List[str], Dict]:
    """
    Last bar must be a confirmation candle WITH volume ≥ VOL_MIN_X.
    Accepted candles: engulfing, marubozu, pin-bar rejection, breakout-close
    past prior 5-bar high (long) / low (short).
    """
    if len(df_daily) < 22:
        return False, [], {"reason": "insufficient_history"}

    o = df_daily['open']
    h = df_daily['high']
    l = df_daily['low']
    c = df_daily['close']
    v = df_daily['volume']

    o_n, h_n, l_n, c_n = (float(o.iloc[-1]), float(h.iloc[-1]),
                          float(l.iloc[-1]), float(c.iloc[-1]))
    o_p, h_p, l_p, c_p = (float(o.iloc[-2]), float(h.iloc[-2]),
                          float(l.iloc[-2]), float(c.iloc[-2]))

    rng_n = max(h_n - l_n, 0.01)
    body_n = abs(c_n - o_n)
    upper_wick = h_n - max(o_n, c_n)
    lower_wick = min(o_n, c_n) - l_n

    patterns: List[str] = []

    # Engulfing
    if direction == "long":
        if c_n > o_n and c_p < o_p and c_n >= o_p and o_n <= c_p:
            patterns.append("bullish_engulfing")
    else:
        if c_n < o_n and c_p > o_p and c_n <= o_p and o_n >= c_p:
            patterns.append("bearish_engulfing")

    # Marubozu (body ≥ 80% of range, in direction)
    if rng_n > 0 and body_n / rng_n >= 0.80:
        if direction == "long" and c_n > o_n:
            patterns.append("bullish_marubozu")
        elif direction == "short" and c_n < o_n:
            patterns.append("bearish_marubozu")

    # Pin bar rejection — wick ≥ 2× body, close in opposite third
    if body_n > 0:
        if direction == "long" and lower_wick >= 2 * body_n and c_n >= (l_n + 0.66 * rng_n):
            patterns.append("bullish_pin_bar")
        elif direction == "short" and upper_wick >= 2 * body_n and c_n <= (h_n - 0.66 * rng_n):
            patterns.append("bearish_pin_bar")

    # Reversal candles already collected (engulfing/marubozu/pin).
    # v3: breakout_5d (9-11% WR in v1 backtest) is now BONUS info, NOT a
    # standalone trigger when precision mode on.
    reversal_present = any(p in {
        "bullish_engulfing", "bearish_engulfing",
        "bullish_marubozu",  "bearish_marubozu",
        "bullish_pin_bar",   "bearish_pin_bar",
    } for p in patterns)

    # Breakout close past prior 5-bar extreme — kept as informational tag only
    prior_high = float(h.iloc[-6:-1].max())
    prior_low = float(l.iloc[-6:-1].min())
    breakout_tag = False
    if direction == "long" and c_n > prior_high:
        patterns.append("breakout_5d_high")
        breakout_tag = True
    elif direction == "short" and c_n < prior_low:
        patterns.append("breakout_5d_low")
        breakout_tag = True

    # v3: close must be in upper/lower 30% of range (strength of close)
    close_strength_ok = True
    if REQUIRE_REVERSAL_CANDLE:
        if direction == "long":
            close_strength_ok = c_n >= (l_n + 0.70 * rng_n)
        else:
            close_strength_ok = c_n <= (h_n - 0.70 * rng_n)

    # Volume gate
    avg_vol = float(v.iloc[-21:-1].mean())
    cur_vol = float(v.iloc[-1])
    vol_ratio = cur_vol / max(avg_vol, 1.0)
    vol_ok = vol_ratio >= VOL_MIN_X

    # v4 factor analysis: a LONE bullish_pin_bar long was 27% WR (vs
    # bullish_marubozu 55%, engulfing 50%). A wicky rejection candle is
    # indecision; a strong-body candle is commitment. Keep pin-only longs ONLY
    # when a real volume surge backs the rejection (institutional), else drop
    # it as a qualifying reversal. Strong-body candles qualify as before.
    if REQUIRE_REVERSAL_CANDLE and direction == "long" and reversal_present:
        strong_body = any(p in ("bullish_engulfing", "bullish_marubozu") for p in patterns)
        pin_only = ("bullish_pin_bar" in patterns) and not strong_body
        if pin_only and vol_ratio < VOL_MIN_X * 1.25:
            reversal_present = False  # weak lone pin — not enough to trade

    # v3: precision mode requires REVERSAL candle (not just breakout) + close strength
    if REQUIRE_REVERSAL_CANDLE:
        has_pattern = reversal_present
    else:
        has_pattern = len(patterns) > 0

    ok = has_pattern and vol_ok and close_strength_ok

    return ok, patterns, {
        "vol_ratio": round(vol_ratio, 2),
        "vol_ok": vol_ok,
        "has_reversal": reversal_present,
        "breakout_tag": breakout_tag,
        "close_strength_ok": close_strength_ok,
        "reason": (
            "ok" if ok
            else ("no_reversal_candle" if not has_pattern
                  else "low_volume" if not vol_ok
                  else "weak_close")
        ),
    }


def gate4_risk(df_daily: pd.DataFrame, direction: str, entry: float) -> Tuple[bool, Dict]:
    """
    Structural stop (swing low/high) + ATR buffer + RR ≥ MIN_RR.

    v2: raw pivot got tagged by intraday noise → 113 SL hits in v1 backtest.
    Now SL = pivot ± PIVOT_ATR_BUFFER × ATR (deeper). Risk still capped 4%.
    """
    atr_val = float(_atr(df_daily, ATR_LEN).iloc[-1])
    if atr_val <= 0 or np.isnan(atr_val):
        return False, {"reason": "bad_atr"}

    max_risk_pct = 0.04

    if direction == "long":
        pivot = _swing_low(df_daily, n=SWING_PIVOT_N, lookback=SWING_LOOKBACK)
        if pivot is None or pivot >= entry:
            return False, {"reason": "no_valid_swing_low", "pivot": pivot}
        # ATR buffer below pivot
        sl = pivot - PIVOT_ATR_BUFFER * atr_val
        # Cap risk to max_risk_pct of entry
        if (entry - sl) / entry > max_risk_pct:
            sl = entry * (1 - max_risk_pct)
        risk = entry - sl
        target = entry + MIN_RR * risk
        return True, {
            "sl": round(sl, 2),
            "pivot": round(pivot, 2),
            "target": round(target, 2),
            "rr": MIN_RR,
            "risk": round(risk, 2),
            "risk_pct": round(risk / entry * 100, 2),
            "atr_buffer": round(PIVOT_ATR_BUFFER * atr_val, 2),
        }
    else:
        pivot = _swing_high(df_daily, n=SWING_PIVOT_N, lookback=SWING_LOOKBACK)
        if pivot is None or pivot <= entry:
            return False, {"reason": "no_valid_swing_high", "pivot": pivot}
        sl = pivot + PIVOT_ATR_BUFFER * atr_val
        if (sl - entry) / entry > max_risk_pct:
            sl = entry * (1 + max_risk_pct)
        risk = sl - entry
        target = entry - MIN_RR * risk
        return True, {
            "sl": round(sl, 2),
            "pivot": round(pivot, 2),
            "target": round(target, 2),
            "rr": MIN_RR,
            "risk": round(risk, 2),
            "risk_pct": round(risk / entry * 100, 2),
            "atr_buffer": round(PIVOT_ATR_BUFFER * atr_val, 2),
        }


def gate5_quality(df_daily: pd.DataFrame, direction: str,
                  nifty_df: Optional[pd.DataFrame] = None) -> Tuple[bool, Dict]:
    """
    Final quality filters: RSI zone, RS vs NIFTY, 52-week proximity.
    """
    info: Dict = {}

    # RSI
    rsi_series = _rsi(df_daily['close'], RSI_LEN)
    rsi = float(rsi_series.iloc[-1])
    info["rsi"] = round(rsi, 1)
    if direction == "long" and not (RSI_LONG_MIN <= rsi <= RSI_LONG_MAX):
        return False, {**info, "reason": f"rsi_{rsi:.0f}_outside_long_zone"}
    # v2: shorts stricter — RSI must be ≤ RS_SHORT_RSI_MAX (40 not 55).
    # Indian market structural up-drift means RSI 40-55 shorts get squeezed.
    if direction == "short" and not (RSI_SHORT_MIN <= rsi <= RS_SHORT_RSI_MAX):
        return False, {**info, "reason": f"rsi_{rsi:.0f}_outside_short_v2_zone"}

    # Relative strength vs NIFTY (20-day)
    if (nifty_df is not None and len(nifty_df) >= 21
            and len(df_daily) >= 21):
        try:
            stock_ret = float(df_daily['close'].iloc[-1] / df_daily['close'].iloc[-21] - 1)
            nifty_ret = float(nifty_df['close'].iloc[-1] / nifty_df['close'].iloc[-21] - 1)
            rs = (1 + stock_ret) / (1 + nifty_ret) if (1 + nifty_ret) != 0 else 1.0
            info["rs_vs_nifty"] = round(rs, 3)
            if direction == "long" and rs < RS_LONG_MIN_VS_NIFTY:
                return False, {**info, "reason": f"weak_RS_{rs:.2f}_long"}
            if direction == "short" and rs > RS_SHORT_MAX_VS_NIFTY:
                return False, {**info, "reason": f"too_strong_RS_{rs:.2f}_for_short"}
        except Exception as e:
            info["rs_warn"] = str(e)

    # 52-week proximity (bonus only, no block)
    if len(df_daily) >= 252:
        yr_high = float(df_daily['high'].iloc[-252:].max())
        yr_low = float(df_daily['low'].iloc[-252:].min())
        last = float(df_daily['close'].iloc[-1])
        if yr_high > 0:
            info["near_52wh"] = (last / yr_high) >= NEAR_52W_HIGH_PCT
        if last > 0:
            info["near_52wl"] = (yr_low / last) >= (1.0 / NEAR_52W_LOW_PCT)

    return True, {**info, "reason": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# Top-level signal generator
# ──────────────────────────────────────────────────────────────────────────

def generate_signal_india_swing(
    symbol: str,
    df_daily: pd.DataFrame,
    nifty_df: Optional[pd.DataFrame] = None,
    as_of_date=None,
) -> Optional[IndiaSwingSignal]:
    """
    Run 5-gate sequential. Return signal only if ALL pass.

    as_of_date: when called from a backtest, pass the trade's evaluation
    date so point-in-time gates (G9 sector_leader) don't peek into the
    future. None = live mode.
    """
    if df_daily is None or df_daily.empty or len(df_daily) < EMA_SLOW + 5:
        return None

    # Normalize column names (yfinance returns 'Close', 'Open', etc.)
    df = df_daily.copy()
    df.columns = [c.lower() for c in df.columns]
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        log.debug(f"[ISW] {symbol} missing columns: have {list(df.columns)}")
        return None

    nf = None
    if nifty_df is not None and not nifty_df.empty:
        nf = nifty_df.copy()
        nf.columns = [c.lower() for c in nf.columns]

    gate_results: Dict[str, bool] = {}

    # ── G1: HTF trend ────────────────────────────────────────────────
    direction, g1 = gate1_htf_trend(df)
    gate_results["g1_trend"] = direction is not None
    if direction is None:
        log.debug(f"[ISW] {symbol} KILL g1: {g1.get('reason')}")
        return None

    # ── Longs-only mode ──────────────────────────────────────────────
    # ONLY_LONG=1 kills all shorts. Data: short WR 12% (n=8) vs long 37% in
    # the v1 CSV. NSE stocks grind up / crash rare+fast — short edge needs a
    # different setup (confirmed ADX-down regime). For the single-setup
    # pullback-continuation thesis, longs only.
    if os.environ.get("ONLY_LONG") == "1" and direction == "short":
        log.debug(f"[ISW] {symbol} KILL short (ONLY_LONG mode)")
        return None

    # ── G0: Market regime (NIFTY) ────────────────────────────────────
    # v3: skip longs in bearish NIFTY, skip shorts in bullish NIFTY.
    g0_ok, g0 = gate0_regime(nf, direction_hint=direction)
    gate_results["g0_regime"] = g0_ok
    if not g0_ok:
        log.debug(f"[ISW] {symbol} KILL g0: {g0.get('reason')}")
        return None

    # ── G2: Pullback ─────────────────────────────────────────────────
    g2_ok, g2 = gate2_pullback(df, direction)
    gate_results["g2_pullback"] = g2_ok
    if not g2_ok:
        log.debug(f"[ISW] {symbol} KILL g2: {g2.get('reason')}")
        return None

    # ── G3: Confirmation ─────────────────────────────────────────────
    g3_ok, patterns, g3 = gate3_confirmation(df, direction)
    gate_results["g3_confirm"] = g3_ok
    if not g3_ok:
        log.debug(f"[ISW] {symbol} KILL g3: {g3.get('reason')} pats={patterns}")
        return None

    # ── G4: Structural risk + RR ─────────────────────────────────────
    entry = float(df['close'].iloc[-1])
    g4_ok, g4 = gate4_risk(df, direction, entry)
    gate_results["g4_risk"] = g4_ok
    if not g4_ok:
        log.debug(f"[ISW] {symbol} KILL g4: {g4.get('reason')}")
        return None

    # ── G5: Quality filters ──────────────────────────────────────────
    g5_ok, g5 = gate5_quality(df, direction, nf)
    gate_results["g5_quality"] = g5_ok
    if not g5_ok:
        log.debug(f"[ISW] {symbol} KILL g5: {g5.get('reason')}")
        return None

    # ── G6: Sector alignment (no fighting sector trend) ──────────────
    try:
        from core.sector_rotation import check_sector_alignment
        g6_ok, g6 = check_sector_alignment(symbol, direction)
        gate_results["g6_sector"] = g6_ok
        if not g6_ok:
            log.debug(f"[ISW] {symbol} KILL g6: {g6.get('reason')}")
            return None
    except Exception as e:
        log.debug(f"[ISW] {symbol} g6 skipped (sector module err): {e}")
        g6 = {"sector_skipped": True}
        gate_results["g6_sector"] = True

    # ── G7: Earnings blackout (no entries within 5d of earnings) ─────
    try:
        from core.earnings_calendar import is_blackout
        blocked, g7 = is_blackout(symbol)
        gate_results["g7_earnings"] = not blocked
        if blocked:
            log.debug(f"[ISW] {symbol} KILL g7: {g7.get('reason')}")
            return None
    except Exception as e:
        log.debug(f"[ISW] {symbol} g7 skipped (earnings module err): {e}")
        g7 = {"earnings_skipped": True}
        gate_results["g7_earnings"] = True

    # ── G8: Delivery % (block weak-delivery longs) ───────────────────
    try:
        from core.delivery_pct import check_delivery
        g8_ok, g8 = check_delivery(symbol, direction)
        gate_results["g8_delivery"] = g8_ok
        if not g8_ok:
            log.debug(f"[ISW] {symbol} KILL g8: {g8.get('reason')}")
            return None
    except Exception as e:
        log.debug(f"[ISW] {symbol} g8 skipped (delivery module err): {e}")
        g8 = {"delivery_skipped": True}
        gate_results["g8_delivery"] = True

    # ── G9: Sector leader (top-3 long, bottom-3 short within sector) ─
    # DISABLE_G9=1 env skips entirely (used during fix-measurement passes
    # so we compare apples-to-apples against the n=230 v3 baseline that
    # predates G9).
    if os.environ.get("DISABLE_G9") == "1":
        gate_results["g9_sector_leader"] = True
        g9 = {"disabled": True}
    else:
        try:
            from core.sector_leader import check_sector_leader
            g9_ok, g9 = check_sector_leader(symbol, direction, as_of_date=as_of_date)
            gate_results["g9_sector_leader"] = g9_ok
            if not g9_ok:
                log.debug(f"[ISW] {symbol} KILL g9: {g9.get('reason')}")
                return None
        except Exception as e:
            log.debug(f"[ISW] {symbol} g9 skipped (sector_leader module err): {e}")
            g9 = {"sector_leader_skipped": True}
            gate_results["g9_sector_leader"] = True

    # ── G3b: standalone-breakout hard-block ──────────────────────────
    # v4 fix #1: breakout_5d_* present in 54% of losers (74/138 in n=230 BT).
    # v3 only penalized -5; that was too soft. Hard-block when breakout is
    # NOT paired with a reversal candle (engulfing/marubozu/pin).
    if (("breakout_5d_high" in patterns or "breakout_5d_low" in patterns)
            and not g3.get("has_reversal")):
        log.debug(f"[ISW] {symbol} KILL standalone breakout (no reversal)")
        return None

    # ── ALL GATES PASSED — build signal ──────────────────────────────
    score = 70.0
    if "bullish_marubozu" in patterns or "bearish_marubozu" in patterns:
        score += 5
    # Breakout PAIRED with reversal = neutral (already gated above).
    if "breakout_5d_high" in patterns or "breakout_5d_low" in patterns:
        pass  # neutral when paired (standalone already killed)
    # v3: 52WH proximity is ANTI-predictive (33% loss rate vs 12% win rate).
    # Flip bonus to penalty when precision mode on.
    if g5.get("near_52wh") and direction == "long":
        score += (-10 if NEAR_52W_HIGH_PENALTY else 10)
    if g5.get("near_52wl") and direction == "short":
        score += (-10 if NEAR_52W_HIGH_PENALTY else 10)
    # RS bonus
    rs = g5.get("rs_vs_nifty")
    if rs is not None:
        if direction == "long" and rs >= 1.10:
            score += 5
        elif direction == "short" and rs <= 0.90:
            score += 5
    # Sector-aligned bonus (bullish sector for long, bearish for short)
    try:
        if isinstance(g6, dict):
            sb = g6.get("bias")
            if (direction == "long" and sb == "bullish") or \
               (direction == "short" and sb == "bearish"):
                score += 5
    except Exception:
        pass
    # High delivery bonus (strong institutional accumulation)
    try:
        if isinstance(g8, dict):
            dp = g8.get("deliv_pct")
            if direction == "long" and dp is not None and dp >= 55:
                score += 5
    except Exception:
        pass
    score = min(score, 100.0)

    grade = "S" if score >= 90 else "A" if score >= 78 else "B"

    atr_val = float(_atr(df, ATR_LEN).iloc[-1])
    # ── G10: ML probability filter (FINAL gate) ─────────────────────
    # Uses everything we've computed (score, RSI, RS, patterns, etc.) to
    # estimate P(win). Pass-through if model not trained yet (cold start).
    # DISABLE_G10=1 env skips entirely (used during fix-measurement passes
    # where G10's circular train-on-self over-filters and masks signal).
    ml_prob: Optional[float] = None
    if os.environ.get("DISABLE_G10") == "1":
        gate_results["g10_ml"] = True
        g10 = {"disabled": True}
    else:
        try:
            from core.ml_filter import check_ml_filter
            candidate_for_ml = {
                "rsi": g5.get("rsi", 0.0),
                "rs_vs_nifty": g5.get("rs_vs_nifty", 1.0) or 1.0,
                "score": score,
                "vol_ratio": g3.get("vol_ratio", 1.0),
                "grade": grade,
                "direction": direction,
                "patterns": patterns,
                "near_52wh": bool(g5.get("near_52wh", False)),
                "near_52wl": bool(g5.get("near_52wl", False)),
            }
            g10_ok, g10 = check_ml_filter(candidate_for_ml)
            gate_results["g10_ml"] = g10_ok
            ml_prob = g10.get("ml_prob")
            if not g10_ok:
                log.debug(f"[ISW] {symbol} KILL g10: {g10.get('reason')}")
                return None
        except Exception as e:
            log.debug(f"[ISW] {symbol} g10 skipped (ml_filter err): {e}")
            gate_results["g10_ml"] = True

    reason = (f"10-gate clean | trend={direction} ema20={g1['ema20']:.1f}>{g1['ema50']:.1f} "
              f"| confirm=[{','.join(patterns)}] vol={g3['vol_ratio']:.1f}x "
              f"| RR={g4['rr']:.1f} risk={g4['risk_pct']:.1f}% "
              f"| RSI={g5.get('rsi'):.0f}"
              + (f" | ML={ml_prob:.2f}" if ml_prob is not None else ""))

    return IndiaSwingSignal(
        symbol=symbol,
        direction=direction,
        entry_price=round(entry, 2),
        sl_price=g4["sl"],
        target_price=g4["target"],
        rr_ratio=g4["rr"],
        confluence_grade=grade,
        confluence_score=score,
        patterns=patterns,
        patterns_combined=patterns,
        reason=reason,
        ts=datetime.now().isoformat(),
        gate_results=gate_results,
        ema20=g1["ema20"],
        ema50=g1["ema50"],
        rsi=g5.get("rsi", 0.0),
        volume_ratio=g3["vol_ratio"],
        atr=atr_val,
        rs_vs_nifty=g5.get("rs_vs_nifty", 0.0) or 0.0,
        near_52wh=bool(g5.get("near_52wh", False)),
        near_52wl=bool(g5.get("near_52wl", False)),
        strength=score,
    )


def scan_universe_india_swing(
    api,
    universe: List[str],
    daily_days: int = 300,
) -> List[Dict]:
    """
    Scan whole universe with India swing strategy. Returns list of dicts
    (legacy Signal shape) compatible with downstream enrichment in
    scan_only_v2.py.

    `api` must expose either get_daily_data(symbol, days) or equivalent.
    Falls back gracefully if NIFTY data unavailable (RS check skipped).
    """
    # ── Top-level regime gate (ADX/ATR) ──────────────────────────────
    # india_swing is trend-following; it bleeds in chop. regime_filter blocks
    # the whole scan when NIFTY ADX < 20 (no trend) or earnings cluster active.
    # This is coarser than the per-signal G0 NIFTY-EMA gate below — it stops
    # the scan entirely instead of filtering direction. DISABLE_REGIME_GATE=1
    # to bypass (e.g. backtest, or to A/B the gate's contribution).
    if os.environ.get("DISABLE_REGIME_GATE") != "1":
        try:
            from core.regime_filter import trade_allowed
            ok, info = trade_allowed("india_swing")
            if not ok:
                log.info(f"[ISW] regime gate BLOCK — {info.get('reason')} "
                         f"(tag={info.get('regime')} adx={info.get('adx')})")
                return []
        except Exception as e:
            log.debug(f"[ISW] regime gate skipped (err): {e}")

    # Fetch NIFTY once for RS calc — cached per scan (Dhan only)
    nifty_df: Optional[pd.DataFrame] = None
    try:
        if hasattr(api, "get_daily_data"):
            nifty_df = api.get_daily_data("NIFTY", days=daily_days)
        else:
            from core.api_dhan import dhan_daily
            nifty_df = dhan_daily("NIFTY", days_back=daily_days)
    except Exception as e:
        log.warning(f"[ISW] NIFTY fetch failed (RS gate skipped): {e}")
        nifty_df = None

    out: List[Dict] = []
    for sym in universe:
        try:
            if hasattr(api, "get_daily_data"):
                df = api.get_daily_data(sym, days=daily_days)
            else:
                from core.api_dhan import dhan_daily
                df = dhan_daily(sym, days_back=daily_days)
            if df is None or df.empty:
                continue
            sig = generate_signal_india_swing(sym, df, nifty_df)
            if sig is not None:
                out.append(sig.to_dict())
        except Exception as e:
            log.debug(f"[ISW] {sym} failed: {e}")
            continue

    # Sort by score desc
    out.sort(key=lambda s: s.get("confluence_score", 0), reverse=True)
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    # Smoke test: synthetic uptrending data
    import pandas as pd
    rng = pd.date_range("2025-01-01", periods=300, freq="B")
    np.random.seed(42)
    base = 100 + np.cumsum(np.random.randn(300) * 0.4) + np.arange(300) * 0.2
    df = pd.DataFrame({
        "open":  base + np.random.randn(300) * 0.1,
        "high":  base + np.abs(np.random.randn(300) * 0.5) + 0.3,
        "low":   base - np.abs(np.random.randn(300) * 0.5) - 0.3,
        "close": base + np.random.randn(300) * 0.1,
        "volume": np.random.randint(100000, 500000, 300),
    }, index=rng)
    # Make last bar a clean bullish engulfing on volume
    df.iloc[-2] = [base[-2] + 0.5, base[-2] + 0.6, base[-2] - 0.4, base[-2] - 0.3, 200000]
    df.iloc[-1] = [base[-1] - 0.2, base[-1] + 1.5, base[-1] - 0.3, base[-1] + 1.2, 450000]
    sig = generate_signal_india_swing("TEST", df)
    print("\n=== SMOKE TEST ===")
    if sig:
        print(f"  Signal:  {sig.symbol} {sig.direction}")
        print(f"  Entry:   {sig.entry_price}  SL: {sig.sl_price}  TGT: {sig.target_price}  RR: {sig.rr_ratio}")
        print(f"  Grade:   {sig.confluence_grade}  Score: {sig.confluence_score}")
        print(f"  Patterns:{sig.patterns}")
        print(f"  Gates:   {sig.gate_results}")
    else:
        print("  No signal (gates rejected)")
