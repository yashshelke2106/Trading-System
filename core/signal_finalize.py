"""
Signal finalisation — the ONE authoritative calibrate + selective-fire
gate, applied AFTER option-leg + OI enrichment.

Why it lives here (not in scan_universe)
----------------------------------------
The gate must see the OI-adjusted confluence_score. If it ran inside
scan_universe (pre-OI) it would kill negative-expectancy candidates
before the OI footprint could rescue or condemn them — defeating the
whole point of the OI edge. So scan_universe just generates ranked
candidates; this runs last, on the fully-enriched dicts, as the single
chokepoint before signals.json.

What it does
------------
1. Map each signal's (OI-adjusted) confluence_score → calibrated P(win).
2. Keep only signals whose expectancy  p·rr − (1−p)  clears a margin.
3. Rank best-edge-first, cap the count. Fewer, better — the only
   honest path to a high *traded* hit rate for an option buyer.
"""

from __future__ import annotations

import logging
from typing import Dict, List

log = logging.getLogger(__name__)

SELECTIVE_FIRE      = True
MIN_EXPECTANCY_R    = 0.15   # need p·rr − (1−p) ≥ this (positive w/ margin)
SELECTIVE_FIRE_KEEP = 12     # hard cap per scan (sniper, not spray)
MIN_VOLUME_RATIO    = 0.70   # vol < 0.7x avg = dead tape, skip

# Pattern conflict sets — if signal has patterns from OPPOSING set, block.
# Empirical: all 3 SL_HITs this week had opposing HTF patterns.
LONG_OPPOSING = {"ema_downtrend", "supertrend_down", "ema_stack_aligned_bear",
                 "ema_bearish_cross", "lower_high_lower_low"}
SHORT_OPPOSING = {"ema_uptrend", "supertrend_up", "ema_stack_aligned_bull",
                  "ema_bullish_cross", "higher_high_higher_low"}


def _has_pattern_conflict(signal: Dict) -> bool:
    """Check if signal has 2+ opposing HTF patterns. Strong SL predictor."""
    direction = signal.get("direction", "long")
    patterns_str = signal.get("patterns_combined", "") or signal.get("patterns", "")
    if not patterns_str:
        return False
    patterns = set(p.strip() for p in patterns_str.split(",") if p.strip())
    opposing = LONG_OPPOSING if direction == "long" else SHORT_OPPOSING
    conflicts = patterns & opposing
    if len(conflicts) >= 2:
        log.info(f"[Conflict] {signal.get('symbol')} {direction} has "
                 f"{len(conflicts)} opposing patterns: {conflicts}")
        return True
    return False


def finalize_and_select(signals: List[Dict]) -> List[Dict]:
    """Calibrate + expectancy-gate a list of enriched signal dicts."""
    if not signals:
        return signals

    # Pre-filter: pattern conflict + volume floor
    pre_count = len(signals)
    filtered = []
    for s in signals:
        # Pattern conflict: 2+ opposing HTF patterns = strong SL predictor
        if _has_pattern_conflict(s):
            continue
        # Volume floor: all SL_HITs had vol < 0.7x, all winners > 1.0x
        vol = s.get("volume_ratio") or s.get("vol_ratio")
        if vol is not None:
            try:
                vol_f = float(vol)
                if vol_f < MIN_VOLUME_RATIO:
                    log.info(f"[VolFloor] {s.get('symbol')} vol={vol_f:.2f} < {MIN_VOLUME_RATIO}")
                    continue
            except (ValueError, TypeError):
                pass
        filtered.append(s)
    if pre_count > len(filtered):
        log.info(f"[PreFilter] {pre_count} -> {len(filtered)} "
                 f"(conflict/vol dropped {pre_count - len(filtered)})")
    signals = filtered

    try:
        from core.calibrator import get_calibrator
        cal = get_calibrator()
    except Exception:
        cal = None

    scored = []
    for s in signals:
        try:
            raw = float(s.get("confluence_score", 0) or 0)
        except (ValueError, TypeError):
            raw = 0.0
        p = 0.0
        if cal is not None:
            try:
                p = float(cal.predict(raw))
            except Exception:
                p = 0.0
        s["calibrated_prob"] = round(p, 4)

        entry = float(s.get("entry_price", 0) or 0)
        sl    = float(s.get("sl_price", 0) or 0)
        tgt   = float(s.get("target_price", 0) or 0)
        sl_d  = abs(entry - sl)
        tgt_d = abs(tgt - entry)
        rr    = (tgt_d / sl_d) if sl_d > 0 else 0.0
        exp_r = p * rr - (1.0 - p) * 1.0
        s["expectancy_r"] = round(exp_r, 4)
        scored.append((s, exp_r))

    if not SELECTIVE_FIRE:
        return [s for s, _ in scored]

    kept = []
    setup_bypass = 0
    for s, e in scored:
        # Setup-matched signals bypass expectancy gate — the combo IS the edge
        if s.get("setup_name") and s.get("setup_type") in ("mega_winner", "high_wr"):
            kept.append((s, max(e, 1.0)))  # force positive expectancy
            setup_bypass += 1
            continue
        if e >= MIN_EXPECTANCY_R:
            kept.append((s, e))

    for s, e in kept:
        s["reason"] = f"{s.get('reason','')} | E={e:+.2f}R p={s['calibrated_prob']:.0%}"
    kept.sort(key=lambda t: t[1], reverse=True)
    out = [s for s, _ in kept[:SELECTIVE_FIRE_KEEP]]
    if setup_bypass:
        log.info(f"[Finalize] {setup_bypass} setup-matched signals bypassed expectancy gate")
    log.info(f"[Finalize] {len(signals)} candidates → {len(out)} fired "
             f"(expectancy ≥ {MIN_EXPECTANCY_R}R)")
    return out
