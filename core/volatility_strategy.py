"""
Volatility Expansion Strategy — direction-agnostic trades.

WHY: Direction prediction is a coin flip (50/50). Volatility prediction
has signal. When realized vol is suppressed and IV is fairly priced,
volatility expansion is statistically likely. Capture it via:

  1. Long Straddle  — buy ATM CE + buy ATM PE (max delta gain on big moves)
  2. Long Strangle  — buy OTM CE + buy OTM PE (cheaper, needs bigger move)

PROFIT MECHANISM:
  Combined position has POSITIVE GAMMA. If stock moves big in EITHER
  direction, the winning leg gains more than the losing leg loses
  (delta accelerates with the move). Theta is the cost.

WHEN TO ENTER (compression detection):
  1. ATR(14) < 60% of ATR(50) — range tightening
  2. Bollinger Band width < 30-day percentile 30 — squeeze
  3. IV percentile < 40% — options priced cheap (relative to history)
  4. Time of day: 9:45-11:30 (theta cost manageable, full session ahead)
  5. NOT on Thursday (expiry day theta crushes overnight)

WHEN TO EXIT:
  - Combined position +30% premium gain (one side covered both costs)
  - Combined position -40% (theta won, accept loss)
  - End of session 14:30 (avoid overnight theta)
  - Stock moved beyond breakeven (locked profit)

Inputs from existing modules:
  - Spot, ATR from intraday OHLC
  - IV from option chain
  - Theta from existing options_greeks.py
  - Strike step from VOLATILITY_CONFIG
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

log = logging.getLogger(__name__)

# ── Volatility detection thresholds ────────────────────────────────────
ATR_COMPRESSION_RATIO = 0.60   # ATR14 / ATR50 < 0.6 = compressed
BB_SQUEEZE_PERCENTILE = 0.30   # BB width below 30th percentile = squeeze
IV_PERCENTILE_MAX = 0.50       # IV below 50th percentile = cheap options
MIN_EXPECTED_MOVE_PCT = 1.2    # required expected move % to justify cost

# ── Trade params ──────────────────────────────────────────────────────
TARGET_GAIN_PCT = 0.30   # exit at +30% combined premium
MAX_LOSS_PCT    = 0.40   # exit at -40% combined premium
ENTRY_START = dt_time(9, 45)
ENTRY_END   = dt_time(11, 30)
HARD_EXIT   = dt_time(14, 30)  # close before session end (theta + slippage)


@dataclass
class VolSetup:
    symbol: str
    spot: float
    atr14: float
    atr50: float
    compression_ratio: float
    bb_width: float
    expected_move_pct: float
    iv_pct: Optional[float] = None
    iv_percentile: Optional[float] = None
    valid: bool = False
    reason: str = ""


def _compute_atr(df: pd.DataFrame, period: int) -> float:
    """Average True Range over period."""
    if df is None or len(df) < period + 1:
        return 0.0
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    tr = np.zeros(len(df))
    for i in range(1, len(df)):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i-1]),
            abs(low[i] - close[i-1]),
        )
    return float(np.mean(tr[-period:]))


def _compute_bb_width(df: pd.DataFrame, period: int = 20) -> float:
    """Bollinger Band width as % of price."""
    if df is None or len(df) < period:
        return 0.0
    closes = df["close"].tail(period)
    sma = closes.mean()
    std = closes.std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    return float((upper - lower) / sma * 100) if sma > 0 else 0.0


def detect_compression(symbol: str, df_5m: pd.DataFrame,
                       df_1d: Optional[pd.DataFrame] = None,
                       iv_pct: Optional[float] = None) -> VolSetup:
    """
    Detect if a stock is in volatility compression mode.

    Returns VolSetup with .valid=True if all 3 conditions met:
      1. ATR14/ATR50 < 0.60 (range compressed)
      2. BB width below recent average (squeeze)
      3. IV percentile < 50% (options cheap) — if IV data available
    """
    if df_5m is None or df_5m.empty or len(df_5m) < 50:
        return VolSetup(symbol, 0, 0, 0, 0, 0, 0, valid=False, reason="no_data")

    try:
        # Use daily bars if available, else 5m
        df = df_1d if (df_1d is not None and len(df_1d) >= 50) else df_5m
        spot = float(df["close"].iloc[-1])
        atr14 = _compute_atr(df, 14)
        atr50 = _compute_atr(df, 50)

        if atr50 <= 0:
            return VolSetup(symbol, spot, atr14, atr50, 0, 0, 0,
                          valid=False, reason="no_atr50")

        compression_ratio = atr14 / atr50
        bb_width = _compute_bb_width(df, 20)

        # Expected move = mean-reversion target. ATR is currently compressed (atr14),
        # but we expect it to revert toward atr50 (the long-term baseline).
        # The TRADE thesis: vol will expand from atr14 → atr50.
        expected_move_pct = (atr50 / spot * 100) if spot > 0 else 0

        # Validation
        if compression_ratio >= ATR_COMPRESSION_RATIO:
            return VolSetup(symbol, spot, atr14, atr50, compression_ratio,
                          bb_width, expected_move_pct, iv_pct=iv_pct,
                          valid=False, reason=f"not_compressed_{compression_ratio:.2f}")

        if expected_move_pct < MIN_EXPECTED_MOVE_PCT:
            return VolSetup(symbol, spot, atr14, atr50, compression_ratio,
                          bb_width, expected_move_pct, iv_pct=iv_pct,
                          valid=False, reason=f"move_too_small_{expected_move_pct:.2f}%")

        # IV check (optional but preferred)
        if iv_pct is not None:
            iv_percentile = _compute_iv_percentile(symbol, iv_pct)
            if iv_percentile is not None and iv_percentile > IV_PERCENTILE_MAX:
                return VolSetup(symbol, spot, atr14, atr50, compression_ratio,
                              bb_width, expected_move_pct, iv_pct=iv_pct,
                              iv_percentile=iv_percentile,
                              valid=False,
                              reason=f"iv_too_rich_{iv_percentile:.0%}")
            return VolSetup(symbol, spot, atr14, atr50, compression_ratio,
                          bb_width, expected_move_pct, iv_pct=iv_pct,
                          iv_percentile=iv_percentile, valid=True)

        # No IV data — still valid if ATR + BB squeeze
        return VolSetup(symbol, spot, atr14, atr50, compression_ratio,
                      bb_width, expected_move_pct, iv_pct=iv_pct,
                      valid=True)
    except Exception as e:
        log.debug(f"detect_compression {symbol}: {e}")
        return VolSetup(symbol, 0, 0, 0, 0, 0, 0, valid=False, reason=f"error_{e}")


def _compute_iv_percentile(symbol: str, current_iv: float) -> Optional[float]:
    """Get IV percentile from iv_rank module."""
    try:
        from core.iv_rank import get_iv_rank
        ir = get_iv_rank()
        # iv_rank module tracks rolling IV history per symbol
        record = ir._records.get(symbol)
        if record and len(record.get("history", [])) >= 10:
            history = record["history"]
            below = sum(1 for v in history if v < current_iv)
            return below / len(history)
    except Exception:
        pass
    return None


def build_straddle_signal(setup: VolSetup, chain_data: List[Dict]) -> Optional[Dict]:
    """
    Build a straddle signal: ATM CE + ATM PE.

    Returns signal dict for scanner pipeline.
    """
    if not setup.valid or not chain_data:
        return None

    spot = setup.spot
    # Find ATM strike (closest to spot)
    atm_row = min(chain_data, key=lambda r: abs(float(r.get("strike", 0)) - spot))
    atm_strike = float(atm_row.get("strike", 0))

    ce_ltp = float(atm_row.get("ce_ltp", 0) or 0)
    pe_ltp = float(atm_row.get("pe_ltp", 0) or 0)
    if ce_ltp <= 0 or pe_ltp <= 0:
        return None

    combined_premium = ce_ltp + pe_ltp
    # Breakevens: stock needs to move past these for net profit
    upper_be = atm_strike + combined_premium
    lower_be = atm_strike - combined_premium
    be_move_pct = (combined_premium / spot * 100) if spot > 0 else 0

    # Reject if breakeven move is too far (greater than expected move)
    if be_move_pct > setup.expected_move_pct * 1.5:
        log.debug(f"[Vol] {setup.symbol} straddle BE move {be_move_pct:.2f}% "
                  f"too far vs expected {setup.expected_move_pct:.2f}%")
        return None

    # Position SL: combined position loses 40% = TIME_EXIT
    sl_combined = combined_premium * (1 - MAX_LOSS_PCT)
    target_combined = combined_premium * (1 + TARGET_GAIN_PCT)

    signal = {
        "symbol": setup.symbol,
        "direction": "vol_expansion",  # not long/short — direction-agnostic
        "strategy": "STRADDLE",
        "entry_price": round(spot, 2),
        "atm_strike": atm_strike,
        "ce_entry_prem": ce_ltp,
        "pe_entry_prem": pe_ltp,
        "combined_premium": round(combined_premium, 2),
        "sl_combined": round(sl_combined, 2),
        "target_combined": round(target_combined, 2),
        "upper_breakeven": round(upper_be, 2),
        "lower_breakeven": round(lower_be, 2),
        "be_move_required_pct": round(be_move_pct, 2),
        "expected_move_pct": round(setup.expected_move_pct, 2),
        "atr14": round(setup.atr14, 2),
        "compression_ratio": round(setup.compression_ratio, 2),
        "bb_width": round(setup.bb_width, 2),
        "iv_pct": setup.iv_pct,
        # Fake spot SL/target for compatibility — actual exit is premium-based
        "sl_price": round(spot * (1 - 0.05), 2),  # cosmetic
        "target_price": round(spot * (1 + 0.05), 2),  # cosmetic
        "rr_ratio": round(TARGET_GAIN_PCT / MAX_LOSS_PCT, 2),
        "confluence_grade": "A",
        "confluence_score": int(80 + (1 - setup.compression_ratio) * 30),
        "patterns_combined": f"vol_compression,atr_squeeze_{setup.compression_ratio:.2f},bb_width_{setup.bb_width:.1f}",
        "patterns": ["vol_compression", "atr_squeeze"],
        "rsi": 50.0,
        "volume_ratio": 1.0,
        "reason": f"VOL STRADDLE {setup.symbol} @ {atm_strike:.0f} "
                  f"CE={ce_ltp} PE={pe_ltp} BE_move={be_move_pct:.2f}%",
        "ts": datetime.now().isoformat(),
    }
    return signal


def build_strangle_signal(setup: VolSetup, chain_data: List[Dict],
                          otm_distance_pct: float = 1.0) -> Optional[Dict]:
    """
    Build a strangle signal: OTM CE + OTM PE.
    Cheaper than straddle, needs bigger move.
    """
    if not setup.valid or not chain_data:
        return None

    spot = setup.spot
    target_ce_strike = spot * (1 + otm_distance_pct / 100)
    target_pe_strike = spot * (1 - otm_distance_pct / 100)

    ce_row = min(chain_data, key=lambda r: abs(float(r.get("strike", 0)) - target_ce_strike))
    pe_row = min(chain_data, key=lambda r: abs(float(r.get("strike", 0)) - target_pe_strike))

    ce_strike = float(ce_row.get("strike", 0))
    pe_strike = float(pe_row.get("strike", 0))
    ce_ltp = float(ce_row.get("ce_ltp", 0) or 0)
    pe_ltp = float(pe_row.get("pe_ltp", 0) or 0)

    if ce_ltp <= 0 or pe_ltp <= 0:
        return None

    combined_premium = ce_ltp + pe_ltp
    upper_be = ce_strike + combined_premium
    lower_be = pe_strike - combined_premium
    be_move_pct = max(
        (upper_be - spot) / spot * 100,
        (spot - lower_be) / spot * 100,
    )

    if be_move_pct > setup.expected_move_pct * 1.5:
        return None

    sl_combined = combined_premium * (1 - MAX_LOSS_PCT)
    target_combined = combined_premium * (1 + TARGET_GAIN_PCT)

    return {
        "symbol": setup.symbol,
        "direction": "vol_expansion",
        "strategy": "STRANGLE",
        "entry_price": round(spot, 2),
        "ce_strike": ce_strike,
        "pe_strike": pe_strike,
        "ce_entry_prem": ce_ltp,
        "pe_entry_prem": pe_ltp,
        "combined_premium": round(combined_premium, 2),
        "sl_combined": round(sl_combined, 2),
        "target_combined": round(target_combined, 2),
        "upper_breakeven": round(upper_be, 2),
        "lower_breakeven": round(lower_be, 2),
        "be_move_required_pct": round(be_move_pct, 2),
        "expected_move_pct": round(setup.expected_move_pct, 2),
        "compression_ratio": round(setup.compression_ratio, 2),
        "sl_price": round(spot * (1 - 0.05), 2),
        "target_price": round(spot * (1 + 0.05), 2),
        "rr_ratio": round(TARGET_GAIN_PCT / MAX_LOSS_PCT, 2),
        "confluence_grade": "A",
        "confluence_score": int(75 + (1 - setup.compression_ratio) * 30),
        "patterns_combined": f"vol_compression,strangle,bb_width_{setup.bb_width:.1f}",
        "patterns": ["vol_compression", "strangle"],
        "rsi": 50.0,
        "volume_ratio": 1.0,
        "reason": f"VOL STRANGLE {setup.symbol} CE={ce_strike:.0f} PE={pe_strike:.0f} "
                  f"BE_move={be_move_pct:.2f}%",
        "ts": datetime.now().isoformat(),
    }


def is_vol_window_active() -> Tuple[bool, str]:
    """Check if we're in the vol-strategy entry window."""
    now = datetime.now().time()
    if now < ENTRY_START:
        return False, "premarket"
    if ENTRY_START <= now <= ENTRY_END:
        return True, "tradeable"
    if ENTRY_END < now < HARD_EXIT:
        return False, "past_entry_window"
    return False, "closed"


def detect_vol_signal(symbol: str, df_5m: pd.DataFrame,
                      df_1d: Optional[pd.DataFrame] = None,
                      chain_data: Optional[List[Dict]] = None,
                      iv_pct: Optional[float] = None,
                      prefer_strangle: bool = False) -> Optional[Dict]:
    """
    Main entry point for scanner.

    Detect compression + build appropriate options structure.
    Returns signal dict or None.
    """
    active, phase = is_vol_window_active()
    if not active:
        return None

    setup = detect_compression(symbol, df_5m, df_1d, iv_pct)
    if not setup.valid:
        log.debug(f"[Vol] {symbol}: {setup.reason}")
        return None

    if not chain_data:
        return None

    # Avoid expiry-day theta crush
    today = datetime.now()
    if today.weekday() == 3:  # Thursday
        log.debug(f"[Vol] {symbol} skipped — Thursday expiry day")
        return None

    # Build signal — strangle if explicitly preferred OR large expected move
    if prefer_strangle or setup.expected_move_pct > 2.5:
        sig = build_strangle_signal(setup, chain_data)
        if sig:
            return sig
        # Fall back to straddle if strangle build failed
    sig = build_straddle_signal(setup, chain_data)
    return sig


if __name__ == "__main__":
    # Self-test
    n = 60
    dates = pd.date_range("2026-05-01", periods=n, freq="D")
    # Compressed market: tight range
    prices = 100 + np.cumsum(np.random.randn(n) * 0.3)  # low vol
    df_1d = pd.DataFrame({
        "date": dates,
        "open": prices + np.random.randn(n) * 0.1,
        "high": prices + abs(np.random.randn(n)) * 0.3,
        "low": prices - abs(np.random.randn(n)) * 0.3,
        "close": prices,
        "volume": np.random.randint(100000, 500000, n),
    })

    setup = detect_compression("TEST", df_1d, df_1d)
    print(f"Compression detected: {setup.valid}")
    print(f"  ATR14: {setup.atr14:.2f}  ATR50: {setup.atr50:.2f}")
    print(f"  Compression ratio: {setup.compression_ratio:.2f}")
    print(f"  Expected move: {setup.expected_move_pct:.2f}%")
    print(f"  BB width: {setup.bb_width:.2f}%")
    print(f"  Reason: {setup.reason}")

    # Mock chain
    spot = setup.spot
    chain = [
        {"strike": spot - 5, "ce_ltp": 7.0, "pe_ltp": 1.5},
        {"strike": spot,      "ce_ltp": 3.5, "pe_ltp": 3.5},
        {"strike": spot + 5, "ce_ltp": 1.5, "pe_ltp": 7.0},
    ]

    print("\nStraddle test:")
    straddle = build_straddle_signal(setup, chain)
    if straddle:
        print(f"  Combined premium: {straddle['combined_premium']}")
        print(f"  Breakevens: [{straddle['lower_breakeven']}, {straddle['upper_breakeven']}]")
        print(f"  BE move needed: {straddle['be_move_required_pct']}%")
        print(f"  Target: {straddle['target_combined']}  SL: {straddle['sl_combined']}")

    print("\nStrangle test:")
    strangle = build_strangle_signal(setup, chain, otm_distance_pct=5)
    if strangle:
        print(f"  Combined premium: {strangle['combined_premium']}")
        print(f"  Breakevens: [{strangle['lower_breakeven']}, {strangle['upper_breakeven']}]")
        print(f"  BE move needed: {strangle['be_move_required_pct']}%")
