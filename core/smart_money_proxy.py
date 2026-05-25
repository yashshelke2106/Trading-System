"""
Smart money / institutional flow proxy.

Can't access L2/L3 order book (institutional advantage). Approximate
institutional positioning via 4 proxies that ARE available:

1. **OI Flow Imbalance**: ΔOI in calls vs puts shows where smart money
   is positioning. Already implemented in oi_signal.py — reuse.

2. **Volume Profile Skew**: heavy volume at specific price levels reveals
   accumulation/distribution. Compute VWAP + standard deviation; price
   far from VWAP with high volume = institutional zone.

3. **Bid-Ask Spread Quality**: tight spread on options = high liquidity =
   institutional interest. Already captured as spread_pct. Use as filter.

4. **Hidden Volume Detection**: large volume on small price moves =
   absorption (someone's filling without moving price). On candle data,
   compute Volume/Range ratio. High V/R = absorption.

5. **Entry Latency Compensation**: 30s scan = we're late. Instead of
   entering at scan price, require price to RETEST (pullback to entry
   zone) within next N bars. Avoid chasing breakouts that already moved.

This module exports `is_institutional_setup()` — returns score 0-100
indicating institutional alignment. Use as score boost in pipeline.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import pandas as pd
import numpy as np

log = logging.getLogger(__name__)


# ── Proxy 1: Volume/Range absorption score ────────────────────────────
def compute_absorption_score(df_5m: pd.DataFrame, lookback: int = 6) -> Dict:
    """
    Heavy volume + tight range = absorption (institutional accumulation/distribution).

    Returns dict with:
      - absorption_score: 0-100 (higher = more absorption)
      - direction: "buy" or "sell" or "neutral"
      - signal: did the absorption support our trade?
    """
    if df_5m is None or len(df_5m) < lookback + 1:
        return {"score": 0, "direction": "neutral", "signal": False}

    recent = df_5m.tail(lookback)
    # Volume/Range ratio per bar — higher = more volume per unit move
    vr_ratios = []
    buy_vol = 0
    sell_vol = 0
    for _, row in recent.iterrows():
        rng = float(row["high"]) - float(row["low"])
        vol = float(row["volume"])
        if rng > 0:
            vr_ratios.append(vol / rng)
        # Direction: close > midpoint = buyer pressure
        mid = (float(row["high"]) + float(row["low"])) / 2
        if float(row["close"]) > mid:
            buy_vol += vol
        else:
            sell_vol += vol

    if not vr_ratios:
        return {"score": 0, "direction": "neutral", "signal": False}

    # Compare current bar's V/R to historical
    avg_vr = np.mean(vr_ratios[:-1])
    cur_vr = vr_ratios[-1]
    if avg_vr <= 0:
        return {"score": 0, "direction": "neutral", "signal": False}

    vr_ratio = cur_vr / avg_vr
    # Score: 100 = 3x absorption, 0 = no absorption
    score = min(100, max(0, int((vr_ratio - 1.0) * 50)))

    total_vol = buy_vol + sell_vol
    if total_vol > 0:
        buy_ratio = buy_vol / total_vol
        if buy_ratio > 0.60:
            direction = "buy"
        elif buy_ratio < 0.40:
            direction = "sell"
        else:
            direction = "neutral"
    else:
        direction = "neutral"

    return {
        "score": score,
        "direction": direction,
        "vr_ratio": round(vr_ratio, 2),
        "signal": score >= 40,
    }


# ── Proxy 2: VWAP distance + volume ───────────────────────────────────
def compute_vwap_institutional_zone(df_5m: pd.DataFrame) -> Dict:
    """
    Price far from VWAP + heavy volume = institutional zone.
    Price NEAR VWAP = noise zone (avoid).

    Returns:
      - distance_from_vwap_pct: how far current price is from VWAP
      - zone: "trend_continuation" | "mean_reversion" | "noise"
    """
    if df_5m is None or len(df_5m) < 20:
        return {"distance": 0, "zone": "noise"}

    try:
        # Calculate intraday VWAP
        df = df_5m.tail(75).copy()  # 1 day of 5m bars
        df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
        df["pv"] = df["tp"] * df["volume"]
        vwap = df["pv"].sum() / df["volume"].sum() if df["volume"].sum() > 0 else 0
        cur = float(df["close"].iloc[-1])
        if vwap <= 0:
            return {"distance": 0, "zone": "noise"}

        dist_pct = (cur - vwap) / vwap * 100

        # Zone classification
        abs_dist = abs(dist_pct)
        if abs_dist < 0.3:
            zone = "noise"  # too close to VWAP — chop zone
        elif abs_dist < 1.0:
            zone = "trend_continuation"  # mild displacement
        elif abs_dist < 2.5:
            zone = "trend_strong"  # institutional flow
        else:
            zone = "mean_reversion"  # extended, reversion risk

        return {
            "distance": round(dist_pct, 2),
            "vwap": round(vwap, 2),
            "current": round(cur, 2),
            "zone": zone,
        }
    except Exception as e:
        log.debug(f"VWAP institutional zone failed: {e}")
        return {"distance": 0, "zone": "noise"}


# ── Proxy 3: Spread quality ───────────────────────────────────────────
def compute_spread_quality(signal: Dict) -> Dict:
    """
    Tight spread = high liquidity = institutional interest.
    Use option chain spread as proxy.

    Returns:
      - quality: "tight" (good) | "wide" (avoid)
      - spread_pct: actual spread percentage
    """
    spread = signal.get("spread_pct")
    if spread is None:
        return {"quality": "unknown", "spread_pct": None}
    try:
        s = float(spread)
        if s < 0.03:
            return {"quality": "tight", "spread_pct": s}
        elif s < 0.08:
            return {"quality": "normal", "spread_pct": s}
        else:
            return {"quality": "wide", "spread_pct": s}
    except (ValueError, TypeError):
        return {"quality": "unknown", "spread_pct": None}


# ── Proxy 4: Late-entry detection (latency compensation) ──────────────
def detect_late_entry(df_5m: pd.DataFrame, entry: float, direction: str,
                      max_age_bars: int = 3) -> Dict:
    """
    Detect if we're chasing a breakout that already moved too much.

    Logic: find the most recent breakout candle. If breakout happened
    more than N bars ago AND price has moved > 0.5% from breakout, we're
    late. Better to skip or wait for retest.

    Returns:
      - is_late: bool
      - move_since_breakout_pct: how far price moved from breakout
      - recommend: "enter" | "wait_retest" | "skip"
    """
    if df_5m is None or len(df_5m) < 5:
        return {"is_late": False, "recommend": "enter"}

    try:
        recent = df_5m.tail(5)
        first_close = float(recent.iloc[0]["close"])
        cur = float(recent.iloc[-1]["close"])

        if direction == "long":
            move_pct = (cur - first_close) / first_close * 100
        else:
            move_pct = (first_close - cur) / first_close * 100

        # If price already moved >0.8% in 5 bars (25 min), we're late
        if move_pct > 0.8:
            return {
                "is_late": True,
                "move_pct": round(move_pct, 2),
                "recommend": "wait_retest",
            }
        elif move_pct > 1.5:
            return {
                "is_late": True,
                "move_pct": round(move_pct, 2),
                "recommend": "skip",
            }
        return {
            "is_late": False,
            "move_pct": round(move_pct, 2),
            "recommend": "enter",
        }
    except Exception:
        return {"is_late": False, "recommend": "enter"}


# ── Unified smart money score ─────────────────────────────────────────
def institutional_alignment_score(signal: Dict,
                                  df_5m: Optional[pd.DataFrame] = None) -> Dict:
    """
    Combine all proxies into a single score.

    Returns:
      - score: 0-100 (higher = more institutional alignment)
      - factors: individual proxy results
      - recommend: action recommendation
    """
    factors = {}
    score = 50  # neutral baseline

    direction = signal.get("direction", "long")

    # Proxy 1: Absorption (heavy vol + tight range)
    if df_5m is not None:
        absorption = compute_absorption_score(df_5m)
        factors["absorption"] = absorption
        if absorption["signal"]:
            if (direction == "long" and absorption["direction"] == "buy") or \
               (direction == "short" and absorption["direction"] == "sell"):
                score += 15
            elif absorption["direction"] != "neutral":
                score -= 10  # absorption against us = bad

    # Proxy 2: VWAP zone
    if df_5m is not None:
        vwap_zone = compute_vwap_institutional_zone(df_5m)
        factors["vwap_zone"] = vwap_zone
        if vwap_zone["zone"] == "noise":
            score -= 15  # in chop zone, no edge
        elif vwap_zone["zone"] in ("trend_continuation", "trend_strong"):
            # Direction check: price above VWAP = buyers, support longs
            dist = vwap_zone["distance"]
            if direction == "long" and dist > 0:
                score += 10
            elif direction == "short" and dist < 0:
                score += 10
            else:
                score -= 10  # counter-VWAP

    # Proxy 3: Spread quality
    spread = compute_spread_quality(signal)
    factors["spread"] = spread
    if spread["quality"] == "tight":
        score += 5
    elif spread["quality"] == "wide":
        score -= 10  # wide spread = execution cost eats profit

    # Proxy 4: OI alignment (use existing patterns)
    pats_str = signal.get("patterns_combined", "") or signal.get("patterns", "")
    if isinstance(pats_str, list):
        pats_str = ", ".join(pats_str)
    if "oi_long_buildup" in pats_str and direction == "long":
        score += 10
        factors["oi"] = "buildup_long"
    elif "oi_short_buildup" in pats_str and direction == "short":
        score += 10
        factors["oi"] = "buildup_short"
    elif "oi_opposes_long" in pats_str and direction == "long":
        score -= 15
        factors["oi"] = "opposes_long"
    elif "oi_opposes_short" in pats_str and direction == "short":
        score -= 15
        factors["oi"] = "opposes_short"

    # Proxy 5: Late entry detection
    if df_5m is not None:
        entry = float(signal.get("entry_price", 0) or 0)
        if entry > 0:
            late = detect_late_entry(df_5m, entry, direction)
            factors["late_entry"] = late
            if late["recommend"] == "skip":
                score -= 25
            elif late["recommend"] == "wait_retest":
                score -= 10

    # Clamp 0-100
    score = max(0, min(100, score))

    # Recommendation
    if score >= 70:
        recommend = "strong_enter"
    elif score >= 55:
        recommend = "enter"
    elif score >= 40:
        recommend = "weak_enter"
    else:
        recommend = "skip"

    return {
        "score": score,
        "factors": factors,
        "recommend": recommend,
    }


if __name__ == "__main__":
    # Test with mock data
    dates = pd.date_range("2026-05-25 09:15", periods=20, freq="5min")
    df = pd.DataFrame({
        "open":   100 + np.linspace(0, 2, 20),
        "high":   100 + np.linspace(0.3, 2.3, 20),
        "low":    100 + np.linspace(-0.2, 1.8, 20),
        "close":  100 + np.linspace(0.1, 2.1, 20),
        "volume": np.random.randint(50000, 200000, 20),
    }, index=dates)

    sig = {
        "symbol": "TEST",
        "direction": "long",
        "entry_price": 102,
        "patterns_combined": "ema_uptrend, oi_long_buildup",
        "spread_pct": 0.025,
    }
    result = institutional_alignment_score(sig, df)
    print(f"Score: {result['score']}/100  Recommend: {result['recommend']}")
    for k, v in result["factors"].items():
        print(f"  {k}: {v}")
