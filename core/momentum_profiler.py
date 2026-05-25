"""
Per-stock momentum indicator profiler.

Instead of global params for all stocks, this module:
  1. Detects real momentum events (2%+ intraday move)
  2. Snapshots ALL indicators at move start
  3. Records which indicators were aligned vs opposing
  4. Builds per-stock "indicator fingerprint"
  5. At signal time, scores how well current indicators match
     the stock's winning fingerprint

Key insight: RELIANCE responds to EMA crossovers, TCS to supertrend,
BAJFINANCE to volume surges. One-size-fits-all params miss this.

Data stored in logs/momentum_profiles.json.
"""

import json
import logging
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

PROFILE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "momentum_profiles.json"
)

# Minimum momentum move to record (% intraday)
MIN_MOVE_PCT = 2.0
# Minimum events before profile is trusted
MIN_EVENTS_FOR_PROFILE = 3
# Maximum events stored per symbol
MAX_EVENTS_PER_SYMBOL = 50

# All indicators we track during momentum events
INDICATOR_SET = [
    # Trend
    "ema_uptrend", "ema_downtrend",
    "ema_bullish_cross", "ema_bearish_cross",
    "ema_stack_aligned_bull", "ema_stack_aligned_bear",
    "ema21_pullback_long", "ema21_pullback_short",
    # Supertrend
    "supertrend_up", "supertrend_down",
    # VWAP
    "above_vwap", "below_vwap",
    "vwap_breakout_up", "vwap_breakout_down",
    # RSI zones
    "rsi_momentum_zone", "rsi_bullish_zone", "rsi_bearish_zone",
    "rsi_short_momentum_zone",
    "rsi_overbought_vol_surge", "rsi_oversold_vol_surge",
    # Price action
    "price_breakout_up", "price_breakout_down",
    "horizontal_breakout_up", "horizontal_breakout_down",
    "bullish_pin_bar", "bearish_pin_bar",
    "bullish_engulfing", "bearish_engulfing",
    "inside_bar_long", "inside_bar_short",
    # WAE / Range
    "wae_bull_explosion", "wae_bear_explosion",
    "range_filter_up", "range_filter_down",
    # Trend structure
    "trend_up", "trend_down",
    "higher_high_higher_low", "lower_high_lower_low",
]


class MomentumProfiler:
    """Learn per-stock indicator fingerprints from real momentum events."""

    def __init__(self):
        self._profiles: Dict[str, Dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(PROFILE_FILE):
            try:
                with open(PROFILE_FILE) as f:
                    self._profiles = json.load(f)
            except Exception as e:
                log.warning(f"[MomProf] load failed: {e}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(PROFILE_FILE), exist_ok=True)
            tmp = PROFILE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._profiles, f, indent=2, default=str)
            os.replace(tmp, PROFILE_FILE)
        except Exception as e:
            log.error(f"[MomProf] save failed: {e}")

    def record_momentum_event(self, symbol: str, direction: str,
                               move_pct: float, patterns: List[str],
                               rsi: float = 0, volume_ratio: float = 0,
                               outcome: str = "unknown") -> None:
        """Record what indicators were present during a momentum event.

        Called when:
          - A signal hits target (proven momentum)
          - A stock moves 2%+ intraday (detected post-hoc)
          - Replay engine finds a real move

        Args:
            symbol: Stock symbol
            direction: 'long' or 'short' — which way did it move
            move_pct: How much did it move (absolute %)
            patterns: List of indicator patterns present at move start
            rsi: RSI at move start
            volume_ratio: Volume ratio at move start
            outcome: 'TARGET_HIT', 'big_move', 'unknown'
        """
        if abs(move_pct) < MIN_MOVE_PCT:
            return

        profile = self._profiles.setdefault(symbol, {
            "long_events": [],
            "short_events": [],
            "indicator_wins": {},   # indicator -> {long: n, short: n}
            "indicator_total": {},  # indicator -> {long: n, short: n}
        })

        event = {
            "direction": direction,
            "move_pct": round(move_pct, 2),
            "patterns": patterns,
            "rsi": round(rsi, 1),
            "volume_ratio": round(volume_ratio, 2),
            "outcome": outcome,
            "ts": time.time(),
        }

        key = f"{direction}_events"
        profile[key].append(event)
        profile[key] = profile[key][-MAX_EVENTS_PER_SYMBOL:]

        # Update indicator tallies
        pattern_set = set(patterns)
        for ind in INDICATOR_SET:
            if ind not in profile["indicator_wins"]:
                profile["indicator_wins"][ind] = {"long": 0, "short": 0}
            if ind not in profile["indicator_total"]:
                profile["indicator_total"][ind] = {"long": 0, "short": 0}

            profile["indicator_total"][ind][direction] += 1
            if ind in pattern_set:
                profile["indicator_wins"][ind][direction] += 1

        self._save()
        log.info(f"[MomProf] {symbol} {direction} {move_pct:+.1f}% "
                 f"recorded ({len(patterns)} indicators)")

    def get_indicator_fingerprint(self, symbol: str,
                                  direction: str) -> Optional[Dict[str, float]]:
        """Return per-indicator hit rate for this stock+direction.

        Returns dict of indicator_name -> probability (0.0 to 1.0).
        Indicators with high probability are the stock's "signature" —
        they're consistently present during real momentum events.

        Returns None if insufficient data.
        """
        profile = self._profiles.get(symbol)
        if not profile:
            return None

        events = profile.get(f"{direction}_events", [])
        if len(events) < MIN_EVENTS_FOR_PROFILE:
            return None

        fingerprint = {}
        for ind in INDICATOR_SET:
            total = profile.get("indicator_total", {}).get(ind, {}).get(direction, 0)
            wins = profile.get("indicator_wins", {}).get(ind, {}).get(direction, 0)
            if total >= MIN_EVENTS_FOR_PROFILE:
                fingerprint[ind] = round(wins / total, 3)

        return fingerprint if fingerprint else None

    def score_signal_fit(self, symbol: str, direction: str,
                          current_patterns: List[str]) -> Optional[float]:
        """Score how well current signal patterns match this stock's fingerprint.

        Returns 0.0-1.0:
          - 1.0 = perfect match (all high-probability indicators present)
          - 0.5 = neutral (no learned profile or average match)
          - 0.0 = anti-match (stock's winning indicators are absent)

        Returns None if insufficient data.
        """
        fingerprint = self.get_indicator_fingerprint(symbol, direction)
        if not fingerprint:
            return None

        # Get indicators with hit rate > 0.5 (present in >50% of events)
        signature_inds = {k: v for k, v in fingerprint.items() if v > 0.5}
        if not signature_inds:
            return None

        # How many signature indicators are present in current signal?
        current_set = set(current_patterns)
        matches = sum(1 for ind in signature_inds if ind in current_set)
        total_sig = len(signature_inds)

        # Also check: are current patterns in the fingerprint at all?
        # Patterns NOT in fingerprint are neutral — stock hasn't shown them
        known_present = sum(1 for p in current_patterns
                           if fingerprint.get(p, 0) > 0.3)
        known_absent = sum(1 for p in current_patterns
                          if fingerprint.get(p, 0) < 0.1 and p in fingerprint)

        # Score: weighted average of signature match + known alignment
        sig_score = matches / total_sig if total_sig > 0 else 0.5
        align_score = (known_present - known_absent * 0.5) / max(len(current_patterns), 1)
        align_score = max(0.0, min(1.0, align_score + 0.5))

        score = 0.6 * sig_score + 0.4 * align_score
        return round(score, 3)

    def get_best_indicators(self, symbol: str, direction: str,
                             top_n: int = 5) -> List[Tuple[str, float]]:
        """Return top N indicators for this stock+direction, by hit rate."""
        fingerprint = self.get_indicator_fingerprint(symbol, direction)
        if not fingerprint:
            return []
        sorted_inds = sorted(fingerprint.items(), key=lambda x: x[1], reverse=True)
        return sorted_inds[:top_n]

    def stats(self) -> Dict:
        """Summary stats for all profiled symbols."""
        result = {}
        for sym, profile in self._profiles.items():
            long_n = len(profile.get("long_events", []))
            short_n = len(profile.get("short_events", []))
            result[sym] = {
                "long_events": long_n,
                "short_events": short_n,
                "total": long_n + short_n,
                "long_top3": self.get_best_indicators(sym, "long", 3),
                "short_top3": self.get_best_indicators(sym, "short", 3),
            }
        return result


_mp: Optional[MomentumProfiler] = None


def get_momentum_profiler() -> MomentumProfiler:
    global _mp
    if _mp is None:
        _mp = MomentumProfiler()
    return _mp


def record_from_journal_entry(entry: Dict) -> None:
    """Record a resolved journal entry as momentum event (if qualified).

    Call after signal outcome is known. Only records TARGET_HIT signals
    and signals with pnl > MIN_MOVE_PCT (proven momentum events).
    """
    outcome = entry.get("outcome", "")
    pnl = entry.get("spot_pnl_pct") or entry.get("pnl_pct") or 0
    try:
        pnl = float(pnl)
    except (ValueError, TypeError):
        pnl = 0

    # Only learn from proven momentum events
    if outcome == "TARGET_HIT" or abs(pnl) >= MIN_MOVE_PCT:
        sym = entry.get("symbol", "")
        direction = entry.get("direction", "long")
        patterns_raw = entry.get("patterns_combined", "") or entry.get("patterns", "")
        if isinstance(patterns_raw, str):
            patterns = [p.strip().strip("'\"") for p in patterns_raw.strip("[]").split(",")
                       if p.strip()]
        else:
            patterns = list(patterns_raw)

        rsi = float(entry.get("rsi", 0) or 0)
        vol = float(entry.get("volume_ratio", 0) or 0)

        get_momentum_profiler().record_momentum_event(
            sym, direction, abs(pnl), patterns, rsi, vol, outcome
        )
