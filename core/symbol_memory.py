"""
Per-symbol memory: each stock has unique behavior.

Tracks per-symbol:
  - Best RSI entry zone (where wins cluster)
  - Worst RSI entry zone (where losses cluster)
  - Best hours for this symbol
  - Optimal SL distance %
  - Pattern preferences (which patterns work for THIS stock)
  - Win rate, total trades

Used by signal/filter agents to refine entry decisions per-symbol.
"""

import json
import logging
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

SYMBOL_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "logs", "symbol_memory.json")

MIN_TRADES_FOR_LEARNED = 8   # need 8+ trades before per-symbol params kick in


class SymbolMemory:
    """Per-symbol learned behavior."""

    def __init__(self):
        self._mem: Dict[str, Dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(SYMBOL_FILE):
            try:
                with open(SYMBOL_FILE) as f:
                    self._mem = json.load(f)
            except Exception as e:
                log.warning(f"[SymMem] load failed: {e}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(SYMBOL_FILE), exist_ok=True)
            tmp = SYMBOL_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._mem, f, default=str)
            os.replace(tmp, SYMBOL_FILE)
        except Exception as e:
            log.error(f"[SymMem] save failed: {e}")

    def record_trade(self, sym: str, outcome: str, rsi: float, hour: int,
                     sl_pct: float, patterns: List[str], pnl: float = 0.0) -> None:
        s = self._mem.setdefault(sym, {
            "trades": [],
            "win_rsi": [], "loss_rsi": [],
            "win_hours": [], "loss_hours": [],
            "win_sl_pct": [], "loss_sl_pct": [],
            "pattern_wins": {}, "pattern_losses": {},
        })

        won = outcome == "TARGET_HIT"
        s["trades"].append({"outcome": outcome, "pnl": pnl, "ts": time.time()})
        s["trades"] = s["trades"][-100:]   # cap

        if won:
            s["win_rsi"].append(rsi)
            s["win_hours"].append(hour)
            s["win_sl_pct"].append(sl_pct)
            for p in patterns:
                s["pattern_wins"][p] = s["pattern_wins"].get(p, 0) + 1
        else:
            s["loss_rsi"].append(rsi)
            s["loss_hours"].append(hour)
            s["loss_sl_pct"].append(sl_pct)
            for p in patterns:
                s["pattern_losses"][p] = s["pattern_losses"].get(p, 0) + 1

        # Cap arrays at 50 entries
        for k in ["win_rsi", "loss_rsi", "win_hours", "loss_hours",
                  "win_sl_pct", "loss_sl_pct"]:
            s[k] = s[k][-50:]

        self._save()

    def get_optimal_rsi_band(self, sym: str) -> Optional[tuple]:
        """Return (low, high) RSI band where this symbol wins. None if insufficient data."""
        s = self._mem.get(sym)
        if not s or len(s.get("win_rsi", [])) < 5:
            return None
        wins = sorted(s["win_rsi"])
        # Use 25th to 75th percentile of winning RSIs
        low = wins[len(wins) // 4]
        high = wins[3 * len(wins) // 4]
        return (low, high)

    def get_optimal_hours(self, sym: str) -> Optional[set]:
        """Return set of hours where this symbol wins. None if insufficient."""
        s = self._mem.get(sym)
        if not s or len(s.get("win_hours", [])) < 5:
            return None
        from collections import Counter
        win_counts = Counter(s["win_hours"])
        loss_counts = Counter(s.get("loss_hours", []))
        good_hours = set()
        for h, w in win_counts.items():
            l = loss_counts.get(h, 0)
            if w + l >= 2 and w / (w + l) >= 0.5:
                good_hours.add(h)
        return good_hours if good_hours else None

    def get_optimal_sl_pct(self, sym: str) -> Optional[float]:
        """Return median winning SL%. None if insufficient."""
        s = self._mem.get(sym)
        if not s or len(s.get("win_sl_pct", [])) < 5:
            return None
        wins = sorted(s["win_sl_pct"])
        return wins[len(wins) // 2]   # median

    def get_pattern_score(self, sym: str, pattern: str) -> Optional[float]:
        """Return WR for this pattern on this symbol. None if insufficient."""
        s = self._mem.get(sym)
        if not s:
            return None
        wins = s.get("pattern_wins", {}).get(pattern, 0)
        losses = s.get("pattern_losses", {}).get(pattern, 0)
        total = wins + losses
        if total < 3:
            return None
        return wins / total

    def is_blacklisted(self, sym: str, threshold_wr: float = 0.20) -> bool:
        """Has this symbol consistently lost? Blacklist if WR < threshold over 8+ trades."""
        s = self._mem.get(sym)
        if not s:
            return False
        trades = s.get("trades", [])
        if len(trades) < MIN_TRADES_FOR_LEARNED:
            return False
        wins = sum(1 for t in trades if t.get("outcome") == "TARGET_HIT")
        wr = wins / len(trades)
        return wr < threshold_wr

    def get_wr(self, sym: str) -> Optional[float]:
        s = self._mem.get(sym)
        if not s or not s.get("trades"):
            return None
        trades = s["trades"]
        wins = sum(1 for t in trades if t.get("outcome") == "TARGET_HIT")
        return wins / len(trades)

    def stats(self) -> Dict:
        return {
            sym: {
                "n": len(s.get("trades", [])),
                "wr": self.get_wr(sym),
                "rsi_band": self.get_optimal_rsi_band(sym),
                "good_hours": list(self.get_optimal_hours(sym) or []),
                "optimal_sl_pct": self.get_optimal_sl_pct(sym),
                "blacklisted": self.is_blacklisted(sym),
            }
            for sym, s in self._mem.items()
        }


_sm: Optional[SymbolMemory] = None


def get_symbol_memory() -> SymbolMemory:
    global _sm
    if _sm is None:
        _sm = SymbolMemory()
    return _sm
