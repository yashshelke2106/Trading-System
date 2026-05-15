"""
Regime-specific parameter sets.

Different market conditions need different params:
  - Strong bull: be aggressive, lower vote threshold, focus longs
  - Bear: tighten everything, allow shorts only with very high conviction
  - Volatile: wider SL needed, take partial profits aggressively
  - Neutral: balanced, default params

Each regime tracked separately. WR computed per-regime. Best params per regime
auto-evolved.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

REGIME_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "logs", "regime_params.json")

# Default params per regime (data-informed starting points)
DEFAULT_REGIME_PARAMS: Dict[str, Dict] = {
    "strong_bull": {
        "min_votes": 3,         # easier signals — strong tape
        "min_strength": 45,
        "rsi_long_momentum_min": 50,
        "rsi_short_floor": 35,  # shorts allowed unless extreme oversold (even in bull, reversals happen)
        "max_volume_ratio": 4.0,
        "rr_ratio": 1.5,
        "disable_shorts": False,
    },
    "bull": {
        "min_votes": 4,
        "min_strength": 50,
        "rsi_long_momentum_min": 55,
        "rsi_short_floor": 32,  # shorts need clear bearish momentum, not just neutral RSI
        "max_volume_ratio": 3.5,
        "rr_ratio": 1.3,
        "disable_shorts": False,
    },
    "neutral": {
        "min_votes": 5,
        "min_strength": 55,
        "rsi_long_momentum_min": 60,
        "rsi_short_floor": 30,  # symmetric: shorts as easy as longs in neutral regime
        "max_volume_ratio": 3.0,
        "rr_ratio": 1.2,
        "disable_shorts": False,
    },
    "bear": {
        "min_votes": 4,         # shorts should be EASIER in bear, not harder
        "min_strength": 50,
        "rsi_long_momentum_min": 65,  # longs need high RSI in bear
        "rsi_short_floor": 25,        # shorts very easy in bear — almost no RSI floor
        "max_volume_ratio": 2.5,
        "rr_ratio": 1.5,
        "disable_shorts": False,
    },
    "strong_bear": {
        "min_votes": 4,
        "min_strength": 45,           # lower bar for shorts in strong bear
        "rsi_long_momentum_min": 68,  # very high bar for longs
        "rsi_short_floor": 20,        # shorts essentially unrestricted in strong bear
        "max_volume_ratio": 2.0,
        "rr_ratio": 1.7,
        "disable_shorts": False,
    },
    "volatile": {
        "min_votes": 5,         # noise filter
        "min_strength": 60,
        "rsi_long_momentum_min": 65,
        "rsi_short_floor": 30,  # was 50 — killed all shorts in volatile. Both directions valid
        "max_volume_ratio": 2.5,
        "rr_ratio": 1.5,
        "atr_sl_multiplier": 1.5,   # wider SL for volatility
        "disable_shorts": False,
    },
}


class RegimeParams:
    """Per-regime param tracking with auto-tuning."""

    def __init__(self):
        self._regimes: Dict[str, Dict] = {}   # regime -> {params, trades, wins}
        self._load()

    def _load(self) -> None:
        if os.path.exists(REGIME_FILE):
            try:
                with open(REGIME_FILE) as f:
                    self._regimes = json.load(f)
            except Exception as e:
                log.warning(f"[Regime] load failed: {e}")

        # Initialize missing regimes from defaults
        for regime, default in DEFAULT_REGIME_PARAMS.items():
            if regime not in self._regimes:
                self._regimes[regime] = {
                    "params": dict(default),
                    "trades": [],
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0,
                }

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(REGIME_FILE), exist_ok=True)
            tmp = REGIME_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._regimes, f, indent=2, default=str)
            os.replace(tmp, REGIME_FILE)
        except Exception as e:
            log.error(f"[Regime] save failed: {e}")

    def get_params(self, regime: str) -> Dict:
        """Return param set for regime. Falls back to neutral."""
        regime = regime if regime in self._regimes else "neutral"
        return dict(self._regimes[regime]["params"])

    def record_trade(self, regime: str, outcome: str, pnl: float, sym: str = "") -> None:
        if regime not in self._regimes:
            regime = "neutral"
        r = self._regimes[regime]
        r["trades"].append({
            "outcome": outcome,
            "pnl": pnl,
            "symbol": sym,
            "ts": time.time(),
        })
        # Keep last 200 trades per regime
        r["trades"] = r["trades"][-200:]
        if outcome == "TARGET_HIT":
            r["wins"] = r.get("wins", 0) + 1
        else:
            r["losses"] = r.get("losses", 0) + 1
        r["total_pnl"] = r.get("total_pnl", 0.0) + pnl
        self._save()

    def get_wr(self, regime: str) -> float:
        if regime not in self._regimes:
            return 0.0
        r = self._regimes[regime]
        n = len(r.get("trades", []))
        if n == 0:
            return 0.0
        wins = sum(1 for t in r["trades"] if t.get("outcome") == "TARGET_HIT")
        return wins / n

    def stats(self) -> Dict:
        return {
            regime: {
                "n": len(r.get("trades", [])),
                "wr": self.get_wr(regime),
                "total_pnl": r.get("total_pnl", 0.0),
                "params": r.get("params", {}),
            }
            for regime, r in self._regimes.items()
        }


_rp: Optional[RegimeParams] = None


def get_regime_params() -> RegimeParams:
    global _rp
    if _rp is None:
        _rp = RegimeParams()
    return _rp
