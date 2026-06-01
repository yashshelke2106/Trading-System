"""
Setup Detector — "Is this THE setup?"

Instead of counting pattern votes (3+ = signal), detect specific
multi-pattern COMBOS proven to produce big wins in journal data.

Data-mined from 338 decided trades in signal_journal.jsonl:
  - 5 mega winners (+40% to +203% premium) shared exact DNA
  - 80% WR combos identified for both long and short
  - 0% WR "loss magnet" combos identified for blocking

Architecture:
  Each Setup is a named combo of required patterns + optional
  conditions (RSI range, volume range). Matched against signal's
  pattern list. Returns setup name, historical WR, and score boost.

  Setup match OVERRIDES generic vote scoring:
    - Matched setup → grade boost (B→A, A→S)
    - Loss magnet match → KILL signal entirely
    - No setup match → normal vote-based scoring (no change)

Auto-updates: re-mines journal weekly via refresh_from_journal().
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JOURNAL = os.path.join(_PROJECT_ROOT, "logs", "signal_journal.jsonl")
_SETUPS_CACHE = os.path.join(_PROJECT_ROOT, "logs", "setup_combos.json")

# Minimum trades to consider a combo statistically meaningful
MIN_COMBO_TRADES = 4
# Minimum WR to qualify as a "good" setup
MIN_SETUP_WR = 0.60
# Maximum WR to qualify as a "loss magnet" (block)
MAX_LOSSMAG_WR = 0.10
# Minimum average PnL for loss magnet blocking
MIN_LOSSMAG_AVG_LOSS = -10.0


@dataclass
class Setup:
    """A named pattern combo with historical stats."""
    name: str
    direction: str                    # "long" or "short"
    required_patterns: Set[str]       # ALL must be present
    win_rate: float                   # 0.0-1.0
    avg_pnl: float                    # average PnL %
    trade_count: int                  # sample size
    big_win_count: int = 0            # trades with PnL > 20%
    score_boost: int = 0              # added to confluence_score
    is_loss_magnet: bool = False      # if True, KILL signal
    rsi_range: Optional[Tuple[float, float]] = None  # optional RSI filter
    max_volume: Optional[float] = None  # optional vol ceiling


# ── HARDCODED SETUPS (from journal mining + mega winner DNA) ────────────
# These fire even if auto-mined cache is empty/stale.

MEGA_WINNER_SETUPS: List[Setup] = [
    # DNA of +40% to +203% winners — THE setup
    Setup(
        name="trend_pullback_explosion",
        direction="long",
        required_patterns={"supertrend_up", "wae_bull_explosion", "ema21_pullback_long", "rsi_momentum_zone"},
        win_rate=0.80, avg_pnl=94.0, trade_count=5, big_win_count=4,
        score_boost=40,
        rsi_range=(50, 75),
    ),
    # Simpler version — 3 of 4 mega winner patterns
    Setup(
        name="trend_pullback_core",
        direction="long",
        required_patterns={"supertrend_up", "ema21_pullback_long", "rsi_momentum_zone"},
        win_rate=0.70, avg_pnl=50.0, trade_count=5, big_win_count=3,
        score_boost=30,
        rsi_range=(50, 80),
    ),
    # WAE + trend alignment
    Setup(
        name="wae_trend_breakout",
        direction="long",
        required_patterns={"wae_bull_explosion", "ema_uptrend", "above_vwap"},
        win_rate=0.65, avg_pnl=30.0, trade_count=5, big_win_count=2,
        score_boost=25,
    ),
]

# ── DATA-MINED HIGH-WR SETUPS ──────────────────────────────────────────

HIGH_WR_SETUPS: List[Setup] = [
    # LONG setups — 60-80% WR from journal
    Setup(
        name="vol_surge_pullback",
        direction="long",
        required_patterns={"ema21_pullback_long", "rsi_bullish_zone", "vol_1.9x"},
        win_rate=0.80, avg_pnl=0.9, trade_count=5,
        score_boost=20,
    ),
    Setup(
        name="supertrend_vol_surge",
        direction="long",
        required_patterns={"supertrend_up", "rsi_bullish_zone", "vol_1.9x"},
        win_rate=0.80, avg_pnl=0.9, trade_count=5,
        score_boost=20,
    ),
    Setup(
        name="breakout_vol_confirmation",
        direction="long",
        required_patterns={"price_breakout_up", "supertrend_up", "vol_1.9x"},
        win_rate=0.75, avg_pnl=0.8, trade_count=4,
        score_boost=15,
    ),
    Setup(
        name="vwap_breakout_stack",
        direction="long",
        required_patterns={"above_vwap", "price_breakout_up", "vwap_breakout_up"},
        win_rate=0.60, avg_pnl=0.4, trade_count=5,
        score_boost=10,
    ),

    # SHORT setups — 75-100% WR from journal
    Setup(
        name="engulfing_momentum_short",
        direction="short",
        required_patterns={"bullish_engulfing", "ema_downtrend", "rsi_short_momentum_zone"},
        win_rate=1.00, avg_pnl=4.2, trade_count=4,
        score_boost=30,
    ),
    Setup(
        name="ema8_cross_downtrend",
        direction="short",
        required_patterns={"ema21_pullback_short", "ema8_fresh_cross_dn", "ema_downtrend"},
        win_rate=0.80, avg_pnl=3.6, trade_count=5,
        score_boost=25,
    ),
    Setup(
        name="engulfing_supertrend_short",
        direction="short",
        required_patterns={"bullish_engulfing", "ema21_pullback_short", "supertrend_down"},
        win_rate=0.80, avg_pnl=3.2, trade_count=5,
        score_boost=20,
    ),
    Setup(
        name="ema8_range_short",
        direction="short",
        required_patterns={"ema21_pullback_short", "ema8_fresh_cross_dn", "range_filter_down"},
        win_rate=0.75, avg_pnl=3.2, trade_count=4,
        score_boost=15,
    ),
    Setup(
        name="pin_momentum_short",
        direction="short",
        required_patterns={"bearish_pin_bar", "ema_downtrend", "rsi_short_momentum_zone"},
        win_rate=0.60, avg_pnl=2.2, trade_count=5,
        score_boost=10,
    ),
]

# ── LOSS MAGNETS — 0% WR, avg PnL < -30% ──────────────────────────────
# If signal matches ANY of these: KILL it.

LOSS_MAGNETS: List[Setup] = [
    Setup(
        name="demand_bounce_trap",
        direction="long",
        required_patterns={"demand_zone_bounce", "rsi_momentum_zone", "supertrend_up"},
        win_rate=0.0, avg_pnl=-42.5, trade_count=5,
        is_loss_magnet=True,
    ),
    Setup(
        name="demand_wae_trap",
        direction="long",
        required_patterns={"demand_zone_bounce", "ema21_pullback_long", "wae_bull_explosion"},
        win_rate=0.0, avg_pnl=-42.5, trade_count=5,
        is_loss_magnet=True,
    ),
    Setup(
        name="uptrend_range_trap",
        direction="long",
        required_patterns={"ema_uptrend", "range_filter_up", "wae_bull_explosion"},
        win_rate=0.0, avg_pnl=-51.8, trade_count=6,
        is_loss_magnet=True,
    ),
    Setup(
        name="supply_bos_trap",
        direction="long",
        required_patterns={"supply_zone_bos_up", "rsi_momentum_zone", "wae_bull_explosion"},
        win_rate=0.0, avg_pnl=-53.2, trade_count=4,
        is_loss_magnet=True,
    ),
    Setup(
        name="engulfing_wae_trap",
        direction="long",
        required_patterns={"bullish_engulfing", "ema_uptrend", "wae_bull_explosion"},
        win_rate=0.0, avg_pnl=-56.6, trade_count=4,
        is_loss_magnet=True,
    ),
    Setup(
        name="pvsra_range_short_trap",
        direction="short",
        required_patterns={"pvsra_super_bear", "range_filter_down", "wae_bear_explosion"},
        win_rate=0.0, avg_pnl=-30.6, trade_count=4,
        is_loss_magnet=True,
    ),
    # Added from 919-trade journal factor analysis (longs): breakout-chasing
    # patterns are the strongest loss magnets — bull_flag_breakout 21% WR
    # (-26pt vs base), horizontal_breakout_up 33%, supply_zone_bos_up 33%.
    # Chasing a breakout long = donating; the edge is the pullback, not the break.
    Setup(
        name="bull_flag_breakout_chase",
        direction="long",
        required_patterns={"bull_flag_breakout"},
        win_rate=0.21, avg_pnl=-30.0, trade_count=14,
        is_loss_magnet=True,
    ),
    Setup(
        name="horizontal_breakout_chase",
        direction="long",
        required_patterns={"horizontal_breakout_up"},
        win_rate=0.33, avg_pnl=-20.0, trade_count=24,
        is_loss_magnet=True,
    ),
]

# Combine all for lookup
ALL_SETUPS = MEGA_WINNER_SETUPS + HIGH_WR_SETUPS + LOSS_MAGNETS


def _parse_patterns(signal: Dict) -> Set[str]:
    """Extract pattern set from signal dict."""
    p = signal.get("patterns_combined", "") or signal.get("patterns", "") or ""
    if isinstance(p, list):
        p = ", ".join(p)
    return set(x.strip() for x in p.split(",") if x.strip() and len(x.strip()) > 2)


def detect_setup(signal: Dict) -> Optional[Dict]:
    """
    Check if signal matches any known setup.

    Returns dict:
      {
        "setup_name": str,
        "setup_type": "mega_winner" | "high_wr" | "loss_magnet",
        "win_rate": float,
        "avg_pnl": float,
        "score_boost": int,       # add to confluence_score
        "is_loss_magnet": bool,   # if True, KILL signal
        "matched_patterns": list, # which patterns matched
      }
    or None if no setup matched.

    Priority: loss_magnets checked first (safety), then mega winners (best),
    then high WR setups.
    """
    direction = signal.get("direction", "long").lower()
    patterns = _parse_patterns(signal)
    rsi = None
    try:
        rsi = float(signal.get("rsi", 0) or 0)
    except (ValueError, TypeError):
        pass
    vol = None
    try:
        vol = float(signal.get("volume_ratio", 0) or signal.get("vol_ratio", 0) or 0)
    except (ValueError, TypeError):
        pass

    if not patterns:
        return None

    # Check loss magnets FIRST — safety
    for setup in LOSS_MAGNETS:
        if setup.direction != direction:
            continue
        if setup.required_patterns.issubset(patterns):
            log.info(f"[Setup] LOSS MAGNET '{setup.name}' matched for "
                     f"{signal.get('symbol')} — KILL "
                     f"(0% WR, avg={setup.avg_pnl:+.1f}%)")
            return {
                "setup_name": setup.name,
                "setup_type": "loss_magnet",
                "win_rate": setup.win_rate,
                "avg_pnl": setup.avg_pnl,
                "score_boost": -50,  # massive penalty
                "is_loss_magnet": True,
                "matched_patterns": list(setup.required_patterns & patterns),
                "trade_count": setup.trade_count,
            }

    # Check mega winner setups
    best_match: Optional[Tuple[Setup, str]] = None
    best_boost = 0

    for setup in MEGA_WINNER_SETUPS:
        if setup.direction != direction:
            continue
        if not setup.required_patterns.issubset(patterns):
            continue
        # RSI range check
        if setup.rsi_range and rsi:
            lo, hi = setup.rsi_range
            if rsi < lo or rsi > hi:
                continue
        if setup.score_boost > best_boost:
            best_match = (setup, "mega_winner")
            best_boost = setup.score_boost

    # Check high WR setups (only if no mega winner found)
    if best_match is None:
        for setup in HIGH_WR_SETUPS:
            if setup.direction != direction:
                continue
            if not setup.required_patterns.issubset(patterns):
                continue
            if setup.rsi_range and rsi:
                lo, hi = setup.rsi_range
                if rsi < lo or rsi > hi:
                    continue
            if setup.score_boost > best_boost:
                best_match = (setup, "high_wr")
                best_boost = setup.score_boost

    # Also check auto-mined setups from cache
    auto_setups = _load_auto_setups()
    if auto_setups and best_match is None:
        for setup in auto_setups:
            if setup.direction != direction:
                continue
            if not setup.required_patterns.issubset(patterns):
                continue
            if setup.score_boost > best_boost:
                best_match = (setup, "auto_mined")
                best_boost = setup.score_boost

    if best_match is None:
        return None

    setup, setup_type = best_match
    log.info(f"[Setup] '{setup.name}' matched for {signal.get('symbol')} "
             f"({setup_type}, WR={setup.win_rate:.0%}, boost={setup.score_boost:+d})")

    return {
        "setup_name": setup.name,
        "setup_type": setup_type,
        "win_rate": setup.win_rate,
        "avg_pnl": setup.avg_pnl,
        "score_boost": setup.score_boost,
        "is_loss_magnet": False,
        "matched_patterns": list(setup.required_patterns & patterns),
        "trade_count": setup.trade_count,
        "big_win_count": setup.big_win_count,
    }


def _load_auto_setups() -> List[Setup]:
    """Load auto-mined setups from cache file."""
    if not os.path.exists(_SETUPS_CACHE):
        return []
    try:
        with open(_SETUPS_CACHE) as f:
            data = json.load(f)
        setups = []
        for d in data.get("setups", []):
            setups.append(Setup(
                name=d["name"],
                direction=d["direction"],
                required_patterns=set(d["required_patterns"]),
                win_rate=d["win_rate"],
                avg_pnl=d["avg_pnl"],
                trade_count=d["trade_count"],
                score_boost=d.get("score_boost", 10),
            ))
        return setups
    except Exception as e:
        log.debug(f"[Setup] auto-setup load failed: {e}")
        return []


def refresh_from_journal(min_trades: int = MIN_COMBO_TRADES,
                         min_wr: float = MIN_SETUP_WR) -> int:
    """Re-mine journal for pattern combos. Save to cache. Returns count found."""
    if not os.path.exists(_JOURNAL):
        return 0

    with open(_JOURNAL) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    clean = [e for e in entries
             if not (e.get("extra", {}) or {}).get("replay_failed")]
    decided = [e for e in clean
               if e.get("outcome") in ("TARGET_HIT", "SL_HIT")]

    if len(decided) < 20:
        return 0

    mined = []
    for direction in ("long", "short"):
        dir_entries = [e for e in decided if e.get("direction") == direction]
        combo_stats: Dict[str, Dict] = defaultdict(
            lambda: {"wins": 0, "total": 0, "pnl_sum": 0})

        for e in dir_entries:
            pats = sorted(_parse_patterns(e))
            pnl = float(e.get("pnl_pct", 0) or 0)
            won = e["outcome"] == "TARGET_HIT"

            for combo in combinations(pats, 3):
                key = "|".join(combo)
                combo_stats[key]["total"] += 1
                if won:
                    combo_stats[key]["wins"] += 1
                combo_stats[key]["pnl_sum"] += pnl

        for key, stats in combo_stats.items():
            if stats["total"] < min_trades:
                continue
            wr = stats["wins"] / stats["total"]
            avg = stats["pnl_sum"] / stats["total"]
            if wr >= min_wr:
                patterns = key.split("|")
                # Skip if already in hardcoded setups
                pat_set = set(patterns)
                already = any(s.required_patterns == pat_set and s.direction == direction
                              for s in ALL_SETUPS)
                if already:
                    continue
                name = f"auto_{direction}_{len(mined)}"
                boost = int(wr * 20)  # 60% WR → +12, 80% → +16
                mined.append({
                    "name": name,
                    "direction": direction,
                    "required_patterns": patterns,
                    "win_rate": round(wr, 3),
                    "avg_pnl": round(avg, 2),
                    "trade_count": stats["total"],
                    "score_boost": boost,
                })

    # Save to cache
    cache = {
        "updated_at": __import__("datetime").datetime.now().isoformat(),
        "source_trades": len(decided),
        "setups": mined,
    }
    try:
        os.makedirs(os.path.dirname(_SETUPS_CACHE), exist_ok=True)
        with open(_SETUPS_CACHE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        log.warning(f"[Setup] cache write failed: {e}")

    log.info(f"[Setup] mined {len(mined)} auto-setups from {len(decided)} trades")
    return len(mined)


def get_all_setups_summary() -> List[Dict]:
    """Return summary of all known setups for dashboard display."""
    results = []
    for s in ALL_SETUPS:
        results.append({
            "name": s.name,
            "direction": s.direction,
            "patterns": sorted(s.required_patterns),
            "win_rate": s.win_rate,
            "avg_pnl": s.avg_pnl,
            "trade_count": s.trade_count,
            "score_boost": s.score_boost,
            "is_loss_magnet": s.is_loss_magnet,
            "type": "loss_magnet" if s.is_loss_magnet else (
                "mega_winner" if s in MEGA_WINNER_SETUPS else "high_wr"),
        })
    # Add auto-mined
    for s in _load_auto_setups():
        results.append({
            "name": s.name,
            "direction": s.direction,
            "patterns": sorted(s.required_patterns),
            "win_rate": s.win_rate,
            "avg_pnl": s.avg_pnl,
            "trade_count": s.trade_count,
            "score_boost": s.score_boost,
            "is_loss_magnet": False,
            "type": "auto_mined",
        })
    return results


if __name__ == "__main__":
    import sys

    if "--mine" in sys.argv:
        n = refresh_from_journal()
        print(f"Mined {n} auto-setups from journal")
        sys.exit(0)

    if "--list" in sys.argv:
        for s in get_all_setups_summary():
            tag = "KILL" if s["is_loss_magnet"] else f"WR={s['win_rate']:.0%}"
            print(f"  [{s['type']:<12}] {s['name']:<30} {s['direction']:<6} "
                  f"{tag:<8} boost={s['score_boost']:>+3} "
                  f"n={s['trade_count']} patterns={s['patterns']}")
        sys.exit(0)

    # Test with a mock signal
    test_signal = {
        "symbol": "TEST",
        "direction": "long",
        "patterns_combined": "supertrend_up, wae_bull_explosion, ema21_pullback_long, rsi_momentum_zone, above_vwap",
        "rsi": 65,
        "volume_ratio": 0.5,
    }
    result = detect_setup(test_signal)
    if result:
        print(f"MATCHED: {result['setup_name']} ({result['setup_type']})")
        print(f"  WR={result['win_rate']:.0%}  boost={result['score_boost']:+d}  "
              f"avg_pnl={result['avg_pnl']:+.1f}%")
    else:
        print("No setup matched")
