"""
Pullback entry system — wait for retest before firing.

PROBLEM: 5m candle pattern detection = breakout already moved 0.5-1%.
By the time scanner fires, price is extended. Chasing = bad fills, wide SL.

SOLUTION: Treat breakout detection as a SETUP, not an entry.
  1. Detected breakout at price X → store as PENDING with retest_zone
  2. Wait for price to pull back into retest_zone (X - 0.3 to 0.5%)
  3. Fire entry when retest happens AND momentum still intact
  4. Time out: if no retest in N scans, drop or downgrade signal

BENEFITS:
  - Better fill price (closer to support/resistance)
  - Tighter SL (just below retest level)
  - Higher R:R (target unchanged, smaller risk)
  - Filters fake breakouts (those that don't retest = exhaustion)

TRADEOFF:
  - Miss strong runners that never pull back (~30% of moves)
  - But strong runners are unpredictable — pullback entries are repeatable

State stored in logs/pending_signals.json. Survives scanner restart.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PENDING_FILE = os.path.join(_PROJECT_ROOT, "logs", "pending_signals.json")

# ── Tunable params ────────────────────────────────────────────────────
PULLBACK_PCT_MIN = 0.30    # require at least 0.3% pullback from setup price
PULLBACK_PCT_MAX = 0.60    # max pullback we accept (beyond this = invalidation)
MAX_PENDING_AGE_SEC = 600  # 10 min — drop pending signals older than this
INVALIDATION_PCT = 1.0     # if price moves +1% AGAINST direction = setup dead
MOMENTUM_CHECK_BARS = 2    # require last N bars to still favor direction


@dataclass
class PendingSignal:
    """A breakout setup waiting for retest confirmation."""
    signal_id: str
    symbol: str
    direction: str
    setup_price: float       # price at setup detection
    setup_ts: str            # ISO timestamp
    retest_low: float        # price at which we'd enter long
    retest_high: float       # price at which we'd enter short
    invalidation_price: float  # price beyond which setup is dead
    sl_price: float          # SL we'd set on entry
    target_price: float      # target we'd set
    original_signal: Dict = field(default_factory=dict)
    age_scans: int = 0       # how many scans we've waited
    max_extension_pct: float = 0.0  # furthest move in direction (for stats)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "PendingSignal":
        return cls(**d)


def build_pending(signal: Dict) -> Optional[PendingSignal]:
    """
    Convert a raw breakout signal into a PendingSignal awaiting retest.

    Returns None if signal can't be converted (missing data).
    Setup-matched signals (mega_winner/high_wr) get a TIGHTER retest zone
    since they're already high-conviction.
    """
    sym = signal.get("symbol")
    direction = signal.get("direction", "").lower()
    entry = float(signal.get("entry_price", 0) or 0)
    sl = float(signal.get("sl_price", 0) or 0)
    target = float(signal.get("target_price", 0) or 0)
    sig_id = signal.get("signal_id") or f"{sym}_{int(time.time())}"

    if not sym or entry <= 0 or sl <= 0 or target <= 0:
        return None

    # Setup-matched = tighter pullback (less waiting needed)
    is_setup = signal.get("setup_type") in ("mega_winner", "high_wr")
    pullback_min = PULLBACK_PCT_MIN * (0.7 if is_setup else 1.0)
    pullback_max = PULLBACK_PCT_MAX * (0.8 if is_setup else 1.0)

    if direction == "long":
        retest_low = entry * (1 - pullback_max / 100)   # max we accept
        retest_high = entry * (1 - pullback_min / 100)  # min pullback required
        invalidation = entry * (1 + INVALIDATION_PCT / 100)  # too high = chasing
    else:  # short
        retest_high = entry * (1 + pullback_max / 100)
        retest_low = entry * (1 + pullback_min / 100)
        invalidation = entry * (1 - INVALIDATION_PCT / 100)

    return PendingSignal(
        signal_id=sig_id,
        symbol=sym,
        direction=direction,
        setup_price=entry,
        setup_ts=signal.get("ts", datetime.now().isoformat()),
        retest_low=retest_low,
        retest_high=retest_high,
        invalidation_price=invalidation,
        sl_price=sl,
        target_price=target,
        original_signal=signal,
    )


def check_retest(pending: PendingSignal, current_price: float,
                 recent_bars: Optional[List[Dict]] = None) -> Tuple[str, str]:
    """
    Check current price against pending signal's retest zone.

    Returns (status, reason):
      status ∈ {"FIRE", "WAIT", "INVALIDATED", "EXTENDED"}
        FIRE         - price in retest zone, momentum OK, enter now
        WAIT         - still waiting for pullback
        INVALIDATED  - price went too far against (setup dead)
        EXTENDED     - price ran away without retesting (missed runner)
    """
    if current_price <= 0:
        return "WAIT", "no_price"

    # Track max extension for stats
    if pending.direction == "long":
        ext = (current_price - pending.setup_price) / pending.setup_price * 100
    else:
        ext = (pending.setup_price - current_price) / pending.setup_price * 100
    if ext > pending.max_extension_pct:
        pending.max_extension_pct = ext

    # Check invalidation (price moved too far against direction)
    if pending.direction == "long":
        # For long, invalidation = price went WAY up (too late, chasing)
        if current_price >= pending.invalidation_price:
            if pending.max_extension_pct > 1.5:
                return "EXTENDED", f"ran_to_+{pending.max_extension_pct:.2f}%"
            return "INVALIDATED", f"price_extended_to_{current_price:.2f}"
    else:
        if current_price <= pending.invalidation_price:
            if pending.max_extension_pct > 1.5:
                return "EXTENDED", f"ran_to_-{pending.max_extension_pct:.2f}%"
            return "INVALIDATED", f"price_extended_to_{current_price:.2f}"

    # Check if SL would already be hit (price went the WRONG way)
    if pending.direction == "long" and current_price <= pending.sl_price:
        return "INVALIDATED", f"sl_already_hit_at_{current_price:.2f}"
    if pending.direction == "short" and current_price >= pending.sl_price:
        return "INVALIDATED", f"sl_already_hit_at_{current_price:.2f}"

    # Check if in retest zone
    if pending.direction == "long":
        in_zone = pending.retest_low <= current_price <= pending.retest_high
    else:
        in_zone = pending.retest_low <= current_price <= pending.retest_high

    if not in_zone:
        return "WAIT", f"price_{current_price:.2f}_not_in_retest_zone"

    # In zone! Check momentum is still aligned (don't enter into crashing price)
    if recent_bars and len(recent_bars) >= MOMENTUM_CHECK_BARS:
        last_n = recent_bars[-MOMENTUM_CHECK_BARS:]
        if pending.direction == "long":
            # Want: recent close >= recent open (at least one green bar)
            recent_close = float(last_n[-1].get("close", 0))
            recent_low = min(float(b.get("low", 999999)) for b in last_n)
            if recent_close <= recent_low * 1.001:
                return "WAIT", "momentum_still_falling"
        else:
            recent_close = float(last_n[-1].get("close", 0))
            recent_high = max(float(b.get("high", 0)) for b in last_n)
            if recent_close >= recent_high * 0.999:
                return "WAIT", "momentum_still_rising"

    return "FIRE", f"retest_at_{current_price:.2f}_pullback_{abs(current_price-pending.setup_price)/pending.setup_price*100:.2f}%"


def adjust_signal_on_fire(pending: PendingSignal, fire_price: float) -> Dict:
    """
    When retest fires, adjust the original signal:
      - New entry = fire_price (the retest price)
      - SL = original SL (or tighter, just below/above retest extreme)
      - Target = original target (preserves runner upside)
      - R:R increases because entry is better
    """
    sig = dict(pending.original_signal)
    sig["entry_price"] = round(fire_price, 2)
    sig["setup_price"] = pending.setup_price
    sig["pullback_pct"] = round(
        abs(fire_price - pending.setup_price) / pending.setup_price * 100, 2
    )

    # Tighten SL: use retest extreme + buffer, BUT enforce 0.8% minimum distance
    # (option premium with delta 1.0 → 0.4% spot move = 40% premium loss = too tight)
    MIN_SL_PCT = 0.008  # 0.8% minimum SL distance
    direction = pending.direction
    if direction == "long":
        retest_sl = pending.retest_low * 0.998  # 0.2% below retest low
        min_sl = fire_price * (1 - MIN_SL_PCT)
        new_sl = min(retest_sl, min_sl)  # take wider of the two (further from entry)
        # Don't go wider than original SL
        new_sl = max(new_sl, pending.sl_price)
    else:
        retest_sl = pending.retest_high * 1.002
        min_sl = fire_price * (1 + MIN_SL_PCT)
        new_sl = max(retest_sl, min_sl)
        new_sl = min(new_sl, pending.sl_price)

    sig["sl_price"] = round(new_sl, 2)
    sig["target_price"] = pending.target_price
    sig["entry_type"] = "pullback_retest"
    sig["original_entry"] = pending.setup_price

    # Recompute R:R
    risk = abs(fire_price - new_sl)
    reward = abs(pending.target_price - fire_price)
    sig["rr_ratio"] = round(reward / risk, 2) if risk > 0 else 0
    sig["reason"] = f'{sig.get("reason","")} | RETEST({pending.age_scans}scans, pb={sig["pullback_pct"]}%)'

    return sig


class PendingQueue:
    """Manages all pending signals waiting for retest."""

    def __init__(self):
        self._pending: Dict[str, PendingSignal] = {}
        self._load()

    def _load(self):
        if not os.path.exists(_PENDING_FILE):
            return
        try:
            with open(_PENDING_FILE) as f:
                data = json.load(f)
            for d in data.get("pending", []):
                p = PendingSignal.from_dict(d)
                self._pending[p.signal_id] = p
            log.info(f"[PullbackQueue] loaded {len(self._pending)} pending signals")
        except Exception as e:
            log.warning(f"[PullbackQueue] load failed: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(_PENDING_FILE), exist_ok=True)
            data = {
                "updated_at": datetime.now().isoformat(),
                "pending": [p.to_dict() for p in self._pending.values()],
            }
            with open(_PENDING_FILE, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            log.warning(f"[PullbackQueue] save failed: {e}")

    def add(self, signal: Dict) -> Optional[PendingSignal]:
        """Add a new breakout setup to the queue (or refresh existing)."""
        pending = build_pending(signal)
        if not pending:
            return None
        # Deduplicate: if symbol+direction already pending today, skip
        for existing in self._pending.values():
            if (existing.symbol == pending.symbol
                    and existing.direction == pending.direction
                    and existing.setup_ts[:10] == pending.setup_ts[:10]):
                return None  # already queued
        self._pending[pending.signal_id] = pending
        log.info(f"[PullbackQueue] +{pending.symbol} {pending.direction} "
                 f"setup={pending.setup_price:.2f} "
                 f"retest=[{pending.retest_low:.2f},{pending.retest_high:.2f}]")
        self._save()
        return pending

    def check_all(self, price_lookup) -> List[Dict]:
        """
        Check all pending signals against current prices.

        price_lookup: callable(symbol) -> (current_price, recent_bars or None)

        Returns list of FIRED signals (ready to execute).
        Mutates queue: removes fired, invalidated, expired entries.
        """
        fired = []
        to_remove = []
        now = datetime.now()

        for sid, pending in list(self._pending.items()):
            pending.age_scans += 1

            # Age check
            try:
                setup_dt = datetime.fromisoformat(pending.setup_ts)
                age_sec = (now - setup_dt).total_seconds()
            except Exception:
                age_sec = 0
            if age_sec > MAX_PENDING_AGE_SEC:
                log.info(f"[PullbackQueue] EXPIRE {pending.symbol} after {age_sec:.0f}s "
                         f"(max_ext={pending.max_extension_pct:.2f}%)")
                to_remove.append(sid)
                continue

            try:
                price, recent_bars = price_lookup(pending.symbol)
            except Exception as e:
                log.debug(f"[PullbackQueue] price lookup {pending.symbol}: {e}")
                continue

            if not price or price <= 0:
                continue

            status, reason = check_retest(pending, price, recent_bars)

            if status == "FIRE":
                fired_signal = adjust_signal_on_fire(pending, price)
                fired.append(fired_signal)
                log.info(f"[PullbackQueue] FIRE {pending.symbol} {pending.direction} "
                         f"@ {price:.2f} (setup was {pending.setup_price:.2f}, "
                         f"pullback={fired_signal['pullback_pct']:.2f}%, "
                         f"R:R={fired_signal['rr_ratio']:.1f})")
                to_remove.append(sid)
            elif status == "INVALIDATED":
                log.info(f"[PullbackQueue] KILL {pending.symbol} — {reason}")
                to_remove.append(sid)
            elif status == "EXTENDED":
                # Setup ran away — log as missed runner stat, drop
                log.info(f"[PullbackQueue] MISS {pending.symbol} — {reason} "
                         f"(strong runner, never retested)")
                to_remove.append(sid)
            # else WAIT — keep in queue

        for sid in to_remove:
            self._pending.pop(sid, None)
        self._save()
        return fired

    def stats(self) -> Dict:
        return {
            "pending_count": len(self._pending),
            "pending_symbols": [p.symbol for p in self._pending.values()],
        }


# Singleton
_queue: Optional[PendingQueue] = None


def get_queue() -> PendingQueue:
    global _queue
    if _queue is None:
        _queue = PendingQueue()
    return _queue


if __name__ == "__main__":
    # Self-test
    sig = {
        "symbol": "TEST",
        "direction": "long",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "target_price": 110.0,
        "ts": datetime.now().isoformat(),
    }

    p = build_pending(sig)
    print(f"Pending: {p.symbol} {p.direction}")
    print(f"  setup_price: {p.setup_price}")
    print(f"  retest zone: [{p.retest_low:.2f}, {p.retest_high:.2f}]")
    print(f"  invalidation: {p.invalidation_price:.2f}")
    print(f"  SL: {p.sl_price}, target: {p.target_price}")

    # Test scenarios
    print("\nScenarios:")
    for test_price in [100.5, 100.0, 99.7, 99.5, 99.3, 101.5, 97.5]:
        status, reason = check_retest(p, test_price)
        print(f"  price {test_price:>6.2f} -> {status:<12} {reason}")
