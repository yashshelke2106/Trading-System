"""
OI snapshot store — the memory that makes ΔOI possible.

The scanner already fetches the full option chain per symbol every cycle
and discards the open-interest. Open interest is *committed positioning*
— the one orthogonal, non-price signal available from data we already
pull. But a single snapshot is static and weak; the edge is the CHANGE
in OI between two points in time vs the change in price (the classic
long-buildup / short-buildup / covering / unwinding map).

This module persists a compact per-symbol summary each scan so the next
scan can diff against it. Append-only JSONL, trimmed to a ring buffer so
it can't grow unbounded. In-memory tail cache so reads are free.

One snapshot row:
  {symbol, ts, spot, total_ce_oi, total_pe_oi, pcr,
   max_ce_oi_strike, max_pe_oi_strike}
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OI_FILE = os.path.join(_PROJECT_ROOT, "logs", "oi_snapshots.jsonl")

KEEP_PER_SYMBOL = 6      # ring-buffer depth per symbol (≈ last 6 scans)


def summarize_chain(symbol: str, chain: List[Dict]) -> Optional[Dict]:
    """Collapse a full chain into the compact OI summary. None if unusable."""
    if not chain:
        return None
    try:
        total_ce = sum(int(r.get("ce_oi", 0) or 0) for r in chain)
        total_pe = sum(int(r.get("pe_oi", 0) or 0) for r in chain)
        if total_ce <= 0 and total_pe <= 0:
            return None
        spot = 0.0
        for r in chain:
            if r.get("_spot"):
                spot = float(r["_spot"])
                break
        # OI walls: strike carrying the most CE OI (resistance) / PE OI (support)
        max_ce = max(chain, key=lambda r: int(r.get("ce_oi", 0) or 0))
        max_pe = max(chain, key=lambda r: int(r.get("pe_oi", 0) or 0))
        pcr = round(total_pe / total_ce, 4) if total_ce > 0 else 0.0
        return {
            "symbol": symbol,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "spot": round(spot, 2),
            "total_ce_oi": total_ce,
            "total_pe_oi": total_pe,
            "pcr": pcr,
            "max_ce_oi_strike": round(float(max_ce.get("strike", 0) or 0), 2),
            "max_pe_oi_strike": round(float(max_pe.get("strike", 0) or 0), 2),
        }
    except Exception as e:
        log.debug(f"[OIStore] summarize {symbol} failed: {e}")
        return None


class OIStore:
    """Per-symbol ring buffer of OI summaries. Singleton via get_oi_store()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mem: Dict[str, Deque[Dict]] = defaultdict(
            lambda: deque(maxlen=KEEP_PER_SYMBOL))
        self._loaded = False

    def _ensure(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_tail()
            self._loaded = True

    def _load_tail(self) -> None:
        if not os.path.exists(OI_FILE):
            return
        try:
            with open(OI_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sym = r.get("symbol")
                    if sym:
                        self._mem[sym].append(r)   # deque keeps last K
        except Exception as e:
            log.warning(f"[OIStore] load failed: {e}")

    def prev(self, symbol: str) -> Optional[Dict]:
        """Most recent PRIOR snapshot for symbol (for ΔOI). None if first."""
        self._ensure()
        dq = self._mem.get(symbol)
        return dq[-1] if dq else None

    def record(self, symbol: str, chain: List[Dict]) -> Optional[Dict]:
        """Summarise + persist a new snapshot. Returns the summary (or None).
        Call AFTER reading prev() if you need the diff."""
        summary = summarize_chain(symbol, chain)
        if summary is None:
            return None
        self._ensure()
        with self._lock:
            try:
                os.makedirs(os.path.dirname(OI_FILE), exist_ok=True)
                with open(OI_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary) + "\n")
            except Exception as e:
                log.warning(f"[OIStore] append failed: {e}")
            self._mem[symbol].append(summary)
        return summary

    def maybe_trim(self, every_lines: int = 5000) -> None:
        """Rewrite the file keeping only the in-memory ring buffer when it
        grows past `every_lines`. Cheap, called opportunistically."""
        try:
            if not os.path.exists(OI_FILE):
                return
            with open(OI_FILE, encoding="utf-8") as f:
                n = sum(1 for _ in f)
            if n < every_lines:
                return
            self._ensure()
            with self._lock:
                tmp = OI_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    for dq in self._mem.values():
                        for r in dq:
                            f.write(json.dumps(r) + "\n")
                os.replace(tmp, OI_FILE)
            log.info(f"[OIStore] trimmed {n} → ring buffer")
        except Exception as e:
            log.debug(f"[OIStore] trim skipped: {e}")

    def status(self) -> Dict:
        self._ensure()
        return {
            "symbols": len(self._mem),
            "keep_per_symbol": KEEP_PER_SYMBOL,
            "with_history": sum(1 for dq in self._mem.values() if len(dq) >= 2),
        }


_store: Optional[OIStore] = None
_store_lock = threading.Lock()


def get_oi_store() -> OIStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = OIStore()
    return _store


if __name__ == "__main__":
    print(json.dumps(get_oi_store().status(), indent=2))
