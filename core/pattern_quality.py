"""
Pattern quality weighting — replaces binary "3+ votes = signal".

Each pattern has different predictive power. RSI crossover ≠ pin_bar ≠ breakout.
This module assigns quality weights based on historical WR per pattern per
direction, mined from signal_journal.jsonl.

Quality formula:
  quality = pattern_win_rate / baseline_win_rate
  - quality > 1.2 = HIGH (predictive edge)
  - quality 0.9-1.2 = NEUTRAL (no edge, doesn't hurt)
  - quality < 0.9 = LOW (anti-edge, hurts conviction)

Total signal quality = mean of pattern qualities.
Used to:
  1. Boost confluence_score for high-quality combos
  2. Penalize signals dominated by low-quality patterns
  3. Auto-refresh from journal weekly

The 3+ votes threshold becomes: 3+ votes WITH avg_quality >= 1.0.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Dict, List, Set, Tuple

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JOURNAL = os.path.join(_PROJECT_ROOT, "logs", "signal_journal.jsonl")
_CACHE = os.path.join(_PROJECT_ROOT, "logs", "pattern_quality.json")

MIN_SAMPLE_PER_PATTERN = 10   # need at least N trades to compute quality
BASELINE_WR_FALLBACK = 0.29   # current system overall WR

# Hardcoded high-quality patterns (data-driven, journal min 10 trades)
# Format: (pattern, direction): quality_multiplier
QUALITY_WEIGHTS_LONG = {
    # HIGH quality (WR > 33%, quality > 1.15x)
    "rsi_bullish_zone":     1.33,
    "bullish_pin_bar":      1.28,
    "bullish_engulfing":    1.22,
    "price_breakout_up":    1.18,
    "vwap_breakout_up":     1.18,
    "supertrend_up":        1.16,
    "ema21_pullback_long":  1.15,
    "ema_uptrend":          1.14,
    "above_vwap":           1.10,
    "wae_bull_explosion":   1.09,
    # NEUTRAL (WR 28-32%)
    "inside_bar_long":      1.03,
    "ema_bullish_cross":    0.96,
    "range_filter_up":      0.94,
    "rsi_momentum_zone":    0.91,
    # LOW (WR < 28%)
    "trend_up":             0.87,
    "rsi_overbought_vol_surge": 0.69,
    "horizontal_breakout_up":   0.29,
}

QUALITY_WEIGHTS_SHORT = {
    # HIGH quality
    "bearish_pin_bar":           1.48,
    "rsi_oversold":              1.19,
    # NEUTRAL
    "rsi_short_momentum_zone":   0.97,
    "ema21_pullback_short":      0.95,
    "trend_down":                0.93,
    "ema_downtrend":             0.91,
    "supertrend_down":           0.90,
    # LOW
    "wae_bear_explosion":        0.86,
    "price_breakout_down":       0.86,
    "inside_bar_short":          0.80,
    "range_filter_down":         0.80,
    "below_vwap":                0.79,
    "bearish_engulfing":         0.78,
    "rsi_bearish_zone":          0.72,
    "pvsra_super_bear":          0.69,
    # VERY LOW (likely failed contexts)
    "vwap_breakout_down":        0.22,
    "ema_bearish_cross":         0.14,
}


def _parse_patterns(signal: Dict) -> Set[str]:
    p = signal.get("patterns_combined", "") or signal.get("patterns", "") or ""
    if isinstance(p, list):
        return set(str(x).strip() for x in p if str(x).strip())
    return set(x.strip() for x in str(p).split(",") if x.strip())


def get_pattern_quality(pattern: str, direction: str,
                        cache: Dict = None) -> float:
    """Return quality multiplier for a single pattern + direction."""
    # Skip volume tag patterns and OI tags — not predictive on their own
    if pattern.startswith("vol_") or pattern.startswith("oi_"):
        return 1.0

    # Check cache first
    if cache:
        d_cache = cache.get(direction, {})
        if pattern in d_cache:
            return d_cache[pattern]

    # Hardcoded fallback
    weights = QUALITY_WEIGHTS_LONG if direction == "long" else QUALITY_WEIGHTS_SHORT
    return weights.get(pattern, 1.0)


def compute_signal_quality(signal: Dict) -> Dict:
    """
    Compute overall signal quality from its patterns.

    Returns:
      {
        "quality":     float,   # mean of pattern qualities (1.0 = neutral)
        "high_q":      int,     # count of patterns with q > 1.15
        "low_q":       int,     # count of patterns with q < 0.85
        "score_adj":   int,     # score adjustment to apply
        "verdict":     "HIGH" | "MID" | "LOW"
      }
    """
    direction = signal.get("direction", "long").lower()
    patterns = _parse_patterns(signal)
    cache = _load_cache()

    if not patterns:
        return {"quality": 1.0, "high_q": 0, "low_q": 0,
                "score_adj": 0, "verdict": "MID"}

    qualities = []
    high_q = 0
    low_q = 0
    for p in patterns:
        if p.startswith("vol_") or p.startswith("oi_"):
            continue  # vol/OI tags don't carry directional quality
        q = get_pattern_quality(p, direction, cache)
        qualities.append(q)
        if q > 1.15:
            high_q += 1
        elif q < 0.85:
            low_q += 1

    if not qualities:
        return {"quality": 1.0, "high_q": 0, "low_q": 0,
                "score_adj": 0, "verdict": "MID"}

    avg_q = sum(qualities) / len(qualities)

    # Score adjustment based on quality
    # avg_q > 1.2 → +15 (high conviction combo)
    # avg_q 0.9-1.2 → 0 (neutral)
    # avg_q < 0.9 → -15 (anti-edge dominant)
    if avg_q >= 1.20:
        score_adj = 20
        verdict = "HIGH"
    elif avg_q >= 1.10:
        score_adj = 10
        verdict = "HIGH"
    elif avg_q >= 0.95:
        score_adj = 0
        verdict = "MID"
    elif avg_q >= 0.85:
        score_adj = -10
        verdict = "LOW"
    else:
        score_adj = -25
        verdict = "LOW"

    # Additional boost: 3+ high-quality patterns = trust the combo
    if high_q >= 3:
        score_adj += 10
    # Penalty: 2+ low-quality patterns dominating
    if low_q >= 2 and high_q <= 1:
        score_adj -= 10

    return {
        "quality": round(avg_q, 3),
        "high_q": high_q,
        "low_q": low_q,
        "score_adj": int(score_adj),
        "verdict": verdict,
    }


def _load_cache() -> Dict:
    """Load journal-mined pattern qualities from cache."""
    if not os.path.exists(_CACHE):
        return {}
    try:
        with open(_CACHE) as f:
            return json.load(f).get("weights", {})
    except Exception:
        return {}


def refresh_from_journal(min_sample: int = MIN_SAMPLE_PER_PATTERN) -> int:
    """Re-mine journal for per-pattern WR per direction. Save to cache."""
    if not os.path.exists(_JOURNAL):
        return 0

    with open(_JOURNAL) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    clean = [e for e in entries
             if not (e.get("extra", {}) or {}).get("replay_failed")]
    decided = [e for e in clean
               if e.get("outcome") in ("TARGET_HIT", "SL_HIT")]

    if len(decided) < 50:
        return 0

    # Overall WR (baseline)
    overall_wr = sum(1 for e in decided if e["outcome"] == "TARGET_HIT") / len(decided)

    # Per-direction per-pattern stats
    stats = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "total": 0}))
    for e in decided:
        d = e.get("direction", "")
        pats = _parse_patterns(e)
        won = e["outcome"] == "TARGET_HIT"
        for p in pats:
            if not p or len(p) < 3 or p.startswith("vol_") or p.startswith("oi_"):
                continue
            stats[d][p]["total"] += 1
            if won:
                stats[d][p]["wins"] += 1

    weights = {"long": {}, "short": {}}
    n_patterns = 0
    for direction in ("long", "short"):
        for p, s in stats[direction].items():
            if s["total"] < min_sample:
                continue
            wr = s["wins"] / s["total"]
            quality = wr / overall_wr if overall_wr > 0 else 1.0
            # Clamp quality to [0.1, 2.0] to avoid extreme outliers
            quality = max(0.1, min(2.0, quality))
            weights[direction][p] = round(quality, 3)
            n_patterns += 1

    cache_data = {
        "updated_at": __import__("datetime").datetime.now().isoformat(),
        "baseline_wr": round(overall_wr, 3),
        "source_trades": len(decided),
        "weights": weights,
    }
    try:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w") as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        log.warning(f"[PatternQuality] cache write failed: {e}")

    log.info(f"[PatternQuality] computed quality for {n_patterns} patterns "
             f"from {len(decided)} trades (baseline WR={overall_wr:.1%})")
    return n_patterns


if __name__ == "__main__":
    import sys
    if "--mine" in sys.argv:
        n = refresh_from_journal()
        print(f"Mined {n} pattern qualities")
        sys.exit(0)

    # Test: high-quality long
    sig_hq = {
        "direction": "long",
        "patterns_combined": "rsi_bullish_zone, bullish_pin_bar, supertrend_up, above_vwap",
    }
    print("HIGH-Q LONG:", compute_signal_quality(sig_hq))

    # Test: low-quality long
    sig_lq = {
        "direction": "long",
        "patterns_combined": "horizontal_breakout_up, rsi_overbought_vol_surge, trend_up",
    }
    print("LOW-Q LONG:", compute_signal_quality(sig_lq))

    # Test: high-quality short
    sig_hs = {
        "direction": "short",
        "patterns_combined": "bearish_pin_bar, rsi_oversold, ema_downtrend",
    }
    print("HIGH-Q SHORT:", compute_signal_quality(sig_hs))

    # Test: bad-quality short
    sig_ls = {
        "direction": "short",
        "patterns_combined": "ema_bearish_cross, vwap_breakout_down, rsi_bullish_zone",
    }
    print("LOW-Q SHORT:", compute_signal_quality(sig_ls))
