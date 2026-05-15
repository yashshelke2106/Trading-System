"""
Signal Memory — tracks signal outcomes for feedback loop.

Every TRADE_CLOSED event records which signal patterns produced that trade,
whether it won, and the R-multiple achieved. This builds a per-symbol,
per-pattern, per-hour hit-rate database that SignalAgent uses to:

  1. Boost/penalize signals based on pattern combo historical WR
  2. Skip signals where pattern combo WR < 25% (over 10+ samples)
  3. Adjust confidence for coordinator voting

Storage: logs/signal_memory.json (persists across restarts)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MEMORY_FILE = os.path.join(_PROJECT_ROOT, "logs", "signal_memory.json")
_LOCK = threading.Lock()

# Minimum samples before signal memory influences decisions
MIN_SAMPLES = 8
# WR below this = skip signal entirely
SKIP_WR_THRESHOLD = 0.20
# WR above this = boost confidence
BOOST_WR_THRESHOLD = 0.50


class SignalMemory:
    """Per-symbol, per-pattern, per-hour outcome tracker."""

    def __init__(self):
        self._data: Dict = {
            "patterns": {},      # "pattern_key" -> {wins, losses, total_r, trades: [...]}
            "symbols": {},       # "SYMBOL" -> {wins, losses, avg_r}
            "hours": {},         # "10" -> {wins, losses}
            "combos": {},        # "pat1+pat2+pat3" -> {wins, losses}
            "updated_at": "",
        }
        self._load()

    def _load(self) -> None:
        with _LOCK:
            try:
                if os.path.exists(_MEMORY_FILE):
                    with open(_MEMORY_FILE, "r", encoding="utf-8") as f:
                        self._data = json.load(f)
                    log.info(f"[SignalMemory] loaded: {sum(v.get('wins',0)+v.get('losses',0) for v in self._data.get('patterns',{}).values())} pattern outcomes")
            except Exception as e:
                log.warning(f"[SignalMemory] load failed: {e}")

    def _save(self) -> None:
        with _LOCK:
            try:
                self._data["updated_at"] = datetime.now().isoformat()
                os.makedirs(os.path.dirname(_MEMORY_FILE), exist_ok=True)
                tmp = _MEMORY_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, _MEMORY_FILE)
            except Exception as e:
                log.warning(f"[SignalMemory] save failed: {e}")

    def record_outcome(
        self,
        symbol: str,
        direction: str,
        patterns: List[str],
        entry_hour: int,
        won: bool,
        r_multiple: float,
        pnl_pct: float,
    ) -> None:
        """Record a trade outcome for feedback learning."""
        key_w = "wins" if won else "losses"

        # Per-pattern tracking
        for pat in patterns:
            pat_key = f"{direction}:{pat}"
            if pat_key not in self._data["patterns"]:
                self._data["patterns"][pat_key] = {"wins": 0, "losses": 0, "total_r": 0.0, "count": 0}
            self._data["patterns"][pat_key][key_w] += 1
            self._data["patterns"][pat_key]["total_r"] += r_multiple
            self._data["patterns"][pat_key]["count"] += 1

        # Pattern combo (sorted for consistency)
        combo_key = "+".join(sorted(f"{direction}:{p}" for p in patterns))
        if combo_key not in self._data["combos"]:
            self._data["combos"][combo_key] = {"wins": 0, "losses": 0, "total_r": 0.0, "count": 0}
        self._data["combos"][combo_key][key_w] += 1
        self._data["combos"][combo_key]["total_r"] += r_multiple
        self._data["combos"][combo_key]["count"] += 1

        # Per-symbol tracking
        if symbol not in self._data["symbols"]:
            self._data["symbols"][symbol] = {"wins": 0, "losses": 0, "total_r": 0.0, "count": 0}
        self._data["symbols"][symbol][key_w] += 1
        self._data["symbols"][symbol]["total_r"] += r_multiple
        self._data["symbols"][symbol]["count"] += 1

        # Per-hour tracking
        hour_key = str(entry_hour)
        if hour_key not in self._data["hours"]:
            self._data["hours"][hour_key] = {"wins": 0, "losses": 0, "count": 0}
        self._data["hours"][hour_key][key_w] += 1
        self._data["hours"][hour_key]["count"] += 1

        self._save()
        log.info(f"[SignalMemory] recorded {symbol} {direction} {'WIN' if won else 'LOSS'} "
                 f"R={r_multiple:.1f} patterns={patterns}")

    def get_pattern_wr(self, direction: str, pattern: str) -> Tuple[float, int]:
        """Return (win_rate, sample_count) for a specific pattern."""
        pat_key = f"{direction}:{pattern}"
        rec = self._data["patterns"].get(pat_key, {})
        w = rec.get("wins", 0)
        l = rec.get("losses", 0)
        total = w + l
        if total == 0:
            return 0.5, 0  # no data = neutral
        return w / total, total

    def get_combo_wr(self, direction: str, patterns: List[str]) -> Tuple[float, int]:
        """Return (win_rate, sample_count) for exact pattern combo."""
        combo_key = "+".join(sorted(f"{direction}:{p}" for p in patterns))
        rec = self._data["combos"].get(combo_key, {})
        w = rec.get("wins", 0)
        l = rec.get("losses", 0)
        total = w + l
        if total == 0:
            return 0.5, 0
        return w / total, total

    def get_symbol_wr(self, symbol: str) -> Tuple[float, int]:
        """Return (win_rate, sample_count) for a symbol."""
        rec = self._data["symbols"].get(symbol, {})
        w = rec.get("wins", 0)
        l = rec.get("losses", 0)
        total = w + l
        if total == 0:
            return 0.5, 0
        return w / total, total

    def get_hour_wr(self, hour: int) -> Tuple[float, int]:
        """Return (win_rate, sample_count) for entry hour."""
        rec = self._data["hours"].get(str(hour), {})
        w = rec.get("wins", 0)
        l = rec.get("losses", 0)
        total = w + l
        if total == 0:
            return 0.5, 0
        return w / total, total

    def should_skip_signal(self, direction: str, patterns: List[str],
                           symbol: str, entry_hour: int) -> Tuple[bool, str]:
        """Check if signal should be skipped based on historical performance.

        Returns (skip: bool, reason: str).
        Only skips if sufficient sample size (MIN_SAMPLES+).
        """
        # Check pattern combo first (most specific)
        combo_wr, combo_n = self.get_combo_wr(direction, patterns)
        if combo_n >= MIN_SAMPLES and combo_wr < SKIP_WR_THRESHOLD:
            return True, f"combo WR={combo_wr:.0%} over {combo_n} trades"

        # Check symbol
        sym_wr, sym_n = self.get_symbol_wr(symbol)
        if sym_n >= MIN_SAMPLES and sym_wr < SKIP_WR_THRESHOLD:
            return True, f"{symbol} WR={sym_wr:.0%} over {sym_n} trades"

        # Check hour
        hour_wr, hour_n = self.get_hour_wr(entry_hour)
        if hour_n >= MIN_SAMPLES * 2 and hour_wr < SKIP_WR_THRESHOLD:
            return True, f"hour {entry_hour} WR={hour_wr:.0%} over {hour_n} trades"

        return False, ""

    def confidence_adjustment(self, direction: str, patterns: List[str],
                               symbol: str, entry_hour: int) -> float:
        """Return confidence multiplier (0.5 - 1.5) based on historical performance.

        > 1.0 = boost (high WR history)
        < 1.0 = penalty (low WR history)
        = 1.0 = neutral (no data or insufficient samples)
        """
        adjustments = []

        # Pattern combo weight (most specific = highest weight)
        combo_wr, combo_n = self.get_combo_wr(direction, patterns)
        if combo_n >= MIN_SAMPLES:
            # Map WR to multiplier: 20%→0.6, 50%→1.0, 70%→1.3
            adj = 0.6 + (combo_wr - 0.2) * 1.4  # linear scale
            adjustments.append(("combo", max(0.5, min(1.5, adj)), combo_n))

        # Individual pattern average
        pat_wrs = []
        for pat in patterns:
            wr, n = self.get_pattern_wr(direction, pat)
            if n >= MIN_SAMPLES // 2:
                pat_wrs.append(wr)
        if pat_wrs:
            avg_pat_wr = sum(pat_wrs) / len(pat_wrs)
            adj = 0.6 + (avg_pat_wr - 0.2) * 1.4
            adjustments.append(("patterns", max(0.5, min(1.5, adj)), len(pat_wrs)))

        # Symbol weight
        sym_wr, sym_n = self.get_symbol_wr(symbol)
        if sym_n >= MIN_SAMPLES:
            adj = 0.7 + (sym_wr - 0.2) * 1.0
            adjustments.append(("symbol", max(0.6, min(1.4, adj)), sym_n))

        # Hour weight
        hour_wr, hour_n = self.get_hour_wr(entry_hour)
        if hour_n >= MIN_SAMPLES:
            adj = 0.7 + (hour_wr - 0.2) * 1.0
            adjustments.append(("hour", max(0.6, min(1.4, adj)), hour_n))

        if not adjustments:
            return 1.0

        # Weighted average: more samples = more trust
        total_weight = sum(n for _, _, n in adjustments)
        weighted = sum(adj * n for _, adj, n in adjustments) / total_weight
        return round(max(0.5, min(1.5, weighted)), 3)

    def get_stats(self) -> Dict:
        """Summary stats for UI/logging."""
        total_patterns = len(self._data["patterns"])
        total_combos = len(self._data["combos"])
        total_symbols = len(self._data["symbols"])

        # Find best/worst patterns
        best = worst = None
        for pat_key, rec in self._data["patterns"].items():
            total = rec.get("wins", 0) + rec.get("losses", 0)
            if total < MIN_SAMPLES:
                continue
            wr = rec["wins"] / total
            if best is None or wr > best[1]:
                best = (pat_key, wr, total)
            if worst is None or wr < worst[1]:
                worst = (pat_key, wr, total)

        return {
            "patterns_tracked": total_patterns,
            "combos_tracked": total_combos,
            "symbols_tracked": total_symbols,
            "best_pattern": {"key": best[0], "wr": f"{best[1]:.0%}", "n": best[2]} if best else None,
            "worst_pattern": {"key": worst[0], "wr": f"{worst[1]:.0%}", "n": worst[2]} if worst else None,
        }


# ── Singleton ────────────────────────────────────────────────────────────────

_instance: Optional[SignalMemory] = None
_instance_lock = threading.Lock()


def get_signal_memory() -> SignalMemory:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = SignalMemory()
    return _instance
