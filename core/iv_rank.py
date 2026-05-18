"""
IV-rank gate — refuse to BUY premium when this symbol's implied vol is
in the top quartile of its own history.

Why
---
An option buyer is long vega. Entering when IV is rich means you pay an
inflated premium and eat the mean-reversion (IV crush) even when spot
goes your way. Across regimes this is a structural loser independent of
direction — and the signal engine is blind to it (it scores price, not
vol). High IV-rank entries silently drag the win rate down.

Data
----
We already journal ``iv_pct`` per signal (BSM/live IV used to price the
option leg). That IS a per-symbol IV time series. IV-rank = percentile
of the current IV within that symbol's own history. No new data feed.

Honest / graceful
-----------------
* Needs ``MIN_HIST`` samples for a symbol before it will block anything
  (thin history → percentile is noise → never block).
* Blocks only the top ``BLOCK_PCTL`` quantile (default 75th).
* Pure read of the journal, cached; refresh() rebuilds.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIN_HIST    = 12     # need this many IV samples for a symbol to trust a rank
BLOCK_PCTL  = 0.75   # block buys at/above this IV percentile (rich vol)


class IVRank:
    """Per-symbol IV history from the journal. Singleton via get_iv_rank()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hist: Dict[str, List[float]] = {}
        self._loaded = False

    def _ensure(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._build()
            self._loaded = True

    def _build(self) -> None:
        hist: Dict[str, List[float]] = {}
        try:
            from core.signal_journal import _load_all
            for r in _load_all():
                iv = r.get("iv_pct")
                sym = r.get("symbol")
                if iv is None or not sym:
                    continue
                try:
                    f = float(iv)
                except (ValueError, TypeError):
                    continue
                if f > 0:
                    hist.setdefault(sym, []).append(f)
        except Exception as e:
            log.warning(f"[IVRank] build failed: {e}")
        self._hist = hist

    def refresh(self) -> None:
        with self._lock:
            self._loaded = False
        self._ensure()

    def rank(self, symbol: str, iv: float) -> Optional[float]:
        """Percentile (0..1) of iv within symbol history.
        None when history too thin to trust."""
        self._ensure()
        try:
            iv = float(iv)
        except (ValueError, TypeError):
            return None
        h = self._hist.get(symbol)
        if not h or len(h) < MIN_HIST or iv <= 0:
            return None
        below = sum(1 for x in h if x <= iv)
        return below / len(h)

    def should_block(self, symbol: str, iv: float,
                     pctl: float = BLOCK_PCTL) -> bool:
        """True only when IV is confidently rich for this symbol."""
        r = self.rank(symbol, iv)
        return r is not None and r >= pctl

    def status(self) -> Dict:
        self._ensure()
        return {
            "symbols": len(self._hist),
            "min_hist": MIN_HIST,
            "block_pctl": BLOCK_PCTL,
            "rich_enough": sum(1 for v in self._hist.values()
                               if len(v) >= MIN_HIST),
        }


_iv: Optional[IVRank] = None
_iv_lock = threading.Lock()


def get_iv_rank() -> IVRank:
    global _iv
    if _iv is None:
        with _iv_lock:
            if _iv is None:
                _iv = IVRank()
    return _iv


if __name__ == "__main__":
    import json
    print(json.dumps(get_iv_rank().status(), indent=2))
