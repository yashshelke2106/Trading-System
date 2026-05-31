"""
Universe filter — trade only the stocks where this setup actually works.

A single common strategy does NOT work equally on all F&O names. The live
journal proves it: NAVINFLUOR 0/8, RAMCOCEM 0/4, MUTHOOTFIN 2/10 are chronic
losers, while RECLTD 4/6 and liquid trenders pull their weight. Choppy,
news-driven, operator-heavy names break a trend-pullback setup no matter how
clean the chart looks.

The fix is NOT a per-stock strategy (that overfits noise). It's one strategy
+ a UNIVERSE FILTER: rank every symbol by how the setup performed on it
historically, keep the winners + the untested (innocent-until-proven), and
block the proven losers.

Decision per symbol (using closed journal trades on this symbol):
  - n < MIN_SAMPLES            -> ALLOW (not enough data to condemn it)
  - win_rate >= WR_FLOOR       -> ALLOW
  - expectancy_R >= 0          -> ALLOW (low WR but positive expectancy ok)
  - else                       -> BLOCK (chronic loser)

Recomputed from logs/signal_journal.jsonl, cached for the session. Cheap.
Spot-based: uses entry_price/exit_price (the real price move), NOT option
premium — so the verdict reflects the setup's edge, not options noise.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_JOURNAL = _ROOT / "logs" / "signal_journal.jsonl"

# Tunables
MIN_SAMPLES = 5      # below this, don't condemn a symbol
WR_FLOOR    = 0.30   # spot win rate floor to stay tradeable
CACHE: Optional[Dict[str, Dict]] = None


def _spot_pnl_pct(r: Dict) -> Optional[float]:
    """Spot % move in the trade's favour (NOT option premium)."""
    try:
        ep = float(r.get("entry_price") or 0)
        xp = float(r.get("exit_price") or 0)
        if ep <= 0 or xp <= 0:
            return None
        if str(r.get("direction", "long")).lower() == "long":
            return (xp - ep) / ep * 100
        return (ep - xp) / ep * 100
    except Exception:
        return None


def _is_junk(r: Dict) -> bool:
    e = r.get("extra") or {}
    return isinstance(e, dict) and bool(e.get("replay_failed"))


def _build() -> Dict[str, Dict]:
    """Per-symbol spot stats from the journal: n, wins, win_rate, expectancy_R,
    and a tradeable verdict. R is approximated from spot pnl / per-trade risk
    where available, else sign of spot pnl."""
    stats: Dict[str, Dict] = {}
    if not _JOURNAL.exists():
        return stats

    rows = defaultdict(list)
    for line in _JOURNAL.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if _is_junk(r):
            continue
        sym = r.get("symbol")
        outcome = str(r.get("outcome", ""))
        # Only count closed, decisive trades
        if not sym or outcome not in ("TARGET_HIT", "SL_HIT", "WIN", "LOSS"):
            continue
        sp = _spot_pnl_pct(r)
        if sp is None:
            continue
        rows[sym.upper()].append(sp)

    for sym, pnls in rows.items():
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n if n else 0.0
        # crude expectancy in % (spot); positive = edge
        exp_pct = sum(pnls) / n if n else 0.0
        if n < MIN_SAMPLES:
            verdict, reason = True, f"untested_n{n}_allow"
        elif wr >= WR_FLOOR:
            verdict, reason = True, f"wr_{wr:.0%}_ok"
        elif exp_pct >= 0:
            verdict, reason = True, f"pos_expectancy_{exp_pct:+.2f}%"
        else:
            verdict, reason = False, f"chronic_loser_wr_{wr:.0%}_exp_{exp_pct:+.2f}%"
        stats[sym] = {
            "n": n, "wins": wins, "win_rate": round(wr, 3),
            "exp_pct": round(exp_pct, 3),
            "tradeable": verdict, "reason": reason,
        }
    return stats


def _ensure() -> Dict[str, Dict]:
    global CACHE
    if CACHE is None:
        CACHE = _build()
    return CACHE


def refresh() -> None:
    """Force rebuild (call after the journal grows materially)."""
    global CACHE
    CACHE = _build()


def is_tradeable(symbol: str) -> Tuple[bool, Dict]:
    """Gate: should we trade this symbol at all?
    Untested / unknown symbols are ALLOWED (innocent until proven)."""
    st = _ensure().get(symbol.upper())
    if st is None:
        return True, {"reason": "no_history_allow", "n": 0}
    return st["tradeable"], st


def blocked_symbols() -> Dict[str, Dict]:
    return {s: v for s, v in _ensure().items() if not v["tradeable"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    st = _ensure()
    if not st:
        print("No journal stats yet.")
    else:
        print(f"Symbols with history: {len(st)}\n")
        print("BLOCKED (chronic losers on spot):")
        for s, v in sorted(blocked_symbols().items(), key=lambda kv: kv[1]["exp_pct"]):
            print(f"  {s:14} n={v['n']:2} wr={v['win_rate']:.0%} exp={v['exp_pct']:+.2f}%  {v['reason']}")
        print("\nTOP allowed (by expectancy):")
        allowed = sorted([(s, v) for s, v in st.items() if v["tradeable"]],
                         key=lambda kv: kv[1]["exp_pct"], reverse=True)
        for s, v in allowed[:12]:
            print(f"  {s:14} n={v['n']:2} wr={v['win_rate']:.0%} exp={v['exp_pct']:+.2f}%")
