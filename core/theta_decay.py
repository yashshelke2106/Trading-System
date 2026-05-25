"""
Theta decay logic for option signals.

Three capabilities:
  1. theta_score_penalty() — penalize signals near expiry / high theta bleed
  2. decaying_roi_target() — time-decay the target premium as holding time grows
  3. premium_exit_check()  — exit when premium hits % gain regardless of spot

FreqTrade ROI table concept adapted for intraday Indian F&O:
  Hour 0-1:  hold for full T2 target
  Hour 1-3:  accept T1.5 (midpoint between T1 and T2)
  Hour 3-5:  accept T1
  Hour 5+:   accept breakeven + spread cost (theta eating everything)

Premium-based exit:
  If premium doubles (100% gain) or hits configurable threshold, exit
  regardless of spot target. Conversely, if premium decays below
  theta-adjusted floor, exit early (theta killing the trade).
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

# ── ROI decay table (hours held → minimum acceptable premium gain %) ────
# Decreasing targets: take profit earlier as theta erodes edge.
# Values are fraction of (target_prem - entry_prem) to accept.
ROI_DECAY_TABLE = [
    (0.0,  1.00),   # first hour: want full T2
    (1.0,  0.85),   # after 1h: accept 85% of target premium gain
    (2.0,  0.65),   # after 2h: accept 65%
    (3.0,  0.50),   # after 3h: accept 50% (≈T1)
    (4.0,  0.35),   # after 4h: accept 35%
    (5.0,  0.20),   # after 5h: accept 20% (barely above breakeven)
    (6.0,  0.10),   # near close: take any profit
]

# Premium exit thresholds
PREMIUM_GAIN_EXIT_PCT  = 0.50   # exit if premium gains 50%+ (was 100%, realistic for ATM)
PREMIUM_LOSS_FLOOR_PCT = 0.30   # exit if premium drops to 30% of entry (theta floor)

# Theta scoring
THETA_DAILY_DECAY_WARN = 0.05   # if daily theta > 5% of premium, penalize
THETA_DAILY_DECAY_KILL = 0.10   # if daily theta > 10% of premium, heavy penalty
MIN_DAYS_TO_EXPIRY     = 1      # signals on expiry day get max penalty
THETA_PENALTY_LIGHT    = -8     # score adjustment for high theta
THETA_PENALTY_HEAVY    = -20    # score adjustment for very high theta


def theta_score_penalty(signal: Dict) -> int:
    """Score penalty based on theta decay rate and days to expiry.

    Called during signal scoring (before grade assignment).
    Returns negative int to subtract from confluence_score.

    Inputs from signal dict:
      - theta:      BSM daily theta (₹ per day, negative)
      - entry_prem: option entry premium (₹)
      - option_expiry: expiry date string (YYYY-MM-DD)
    """
    theta = signal.get("theta")
    entry_prem = signal.get("entry_prem")
    expiry_str = signal.get("option_expiry")

    if theta is None or entry_prem is None:
        return 0

    try:
        ep = float(entry_prem)
        th = abs(float(theta))
    except (ValueError, TypeError):
        return 0

    if ep <= 0:
        return 0

    # Daily decay as fraction of premium
    daily_decay_pct = th / ep

    penalty = 0

    # Theta rate penalty
    if daily_decay_pct >= THETA_DAILY_DECAY_KILL:
        penalty += THETA_PENALTY_HEAVY
        log.debug(f"[Theta] {signal.get('symbol')} decay={daily_decay_pct:.1%} "
                  f"of premium/day → heavy penalty {THETA_PENALTY_HEAVY}")
    elif daily_decay_pct >= THETA_DAILY_DECAY_WARN:
        penalty += THETA_PENALTY_LIGHT
        log.debug(f"[Theta] {signal.get('symbol')} decay={daily_decay_pct:.1%} "
                  f"of premium/day → light penalty {THETA_PENALTY_LIGHT}")

    # Days to expiry penalty
    if expiry_str:
        try:
            expiry_date = date.fromisoformat(expiry_str)
            days_left = (expiry_date - date.today()).days
            if days_left <= 0:
                penalty += THETA_PENALTY_HEAVY  # expiry day: max bleed
                log.debug(f"[Theta] {signal.get('symbol')} expiry TODAY → "
                          f"penalty {THETA_PENALTY_HEAVY}")
            elif days_left <= MIN_DAYS_TO_EXPIRY:
                penalty += THETA_PENALTY_LIGHT
                log.debug(f"[Theta] {signal.get('symbol')} {days_left}d to expiry → "
                          f"penalty {THETA_PENALTY_LIGHT}")
        except (ValueError, TypeError):
            pass

    return penalty


def decaying_target_prem(entry_prem: float, target_prem: float,
                         hours_held: float) -> float:
    """Time-decay the target premium as holding time increases.

    Returns the MINIMUM premium at which we should take profit.
    As hours_held increases, we accept smaller gains.

    Example:
      entry=100, target=150 (50% gain)
      Hour 0: need 150 (full target)
      Hour 2: need 132.5 (65% of 50 gain = 32.5 above entry)
      Hour 5: need 110 (20% of 50 gain = 10 above entry)
    """
    if entry_prem <= 0 or target_prem <= entry_prem:
        return target_prem

    gain = target_prem - entry_prem

    # Interpolate ROI table
    frac = 1.0  # default: want full target
    for i, (h, f) in enumerate(ROI_DECAY_TABLE):
        if hours_held <= h:
            if i == 0:
                frac = f
            else:
                h_prev, f_prev = ROI_DECAY_TABLE[i - 1]
                # Linear interpolation between table entries
                t = (hours_held - h_prev) / (h - h_prev) if h > h_prev else 0.0
                frac = f_prev + t * (f - f_prev)
            break
    else:
        # Past last table entry: use last value
        frac = ROI_DECAY_TABLE[-1][1]

    decayed_target = entry_prem + gain * frac
    return round(decayed_target, 2)


def premium_exit_check(entry_prem: float, current_prem: float,
                       hours_held: float = 0.0,
                       target_prem: float = 0.0,
                       sl_prem: float = 0.0) -> Tuple[Optional[str], str]:
    """Check if premium warrants exit regardless of spot price.

    Returns:
      (outcome, reason) where outcome is:
        "TARGET_HIT" — premium gain threshold reached
        "SL_HIT"     — premium dropped to theta floor
        None         — no exit signal

    Three exit triggers:
      1. Premium gained PREMIUM_GAIN_EXIT_PCT → take profit
      2. Premium dropped to PREMIUM_LOSS_FLOOR_PCT of entry → theta floor exit
      3. Decayed target reached (time-adjusted partial profit)
    """
    if entry_prem <= 0 or current_prem <= 0:
        return None, ""

    prem_change_pct = (current_prem - entry_prem) / entry_prem

    # 1. Big premium gain → take profit (option doubled or hit threshold)
    if prem_change_pct >= PREMIUM_GAIN_EXIT_PCT:
        return "TARGET_HIT", f"premium_gain={prem_change_pct:.0%}"

    # 2. Premium collapsed to floor → theta/IV crushed it, cut loss
    prem_ratio = current_prem / entry_prem
    if prem_ratio <= PREMIUM_LOSS_FLOOR_PCT:
        return "SL_HIT", f"premium_floor={prem_ratio:.0%}_of_entry"

    # 3. Time-decayed target: accept smaller gain as time passes
    if target_prem > 0 and hours_held > 0.5:
        decayed = decaying_target_prem(entry_prem, target_prem, hours_held)
        if current_prem >= decayed and current_prem > entry_prem:
            return "TARGET_HIT", f"decayed_target={decayed:.1f}_at_{hours_held:.1f}h"

    return None, ""


def estimate_premium_at_hours(entry_prem: float, theta: float,
                              delta: float, spot_move_pct: float,
                              hours: float, entry_spot: float = 0.0) -> float:
    """Project premium after N hours given theta decay and spot movement.

    Simple model: premium = entry + delta*spot_move - theta*hours/24
    Used for signal scoring to estimate if target is reachable before
    theta eats the edge.
    """
    if entry_prem <= 0:
        return entry_prem

    try:
        th = abs(float(theta))
        d = abs(float(delta))
    except (ValueError, TypeError):
        return entry_prem

    # Theta decay over holding period (theta is daily, convert to hours)
    theta_cost = th * (hours / 24.0)

    # Delta gain from spot movement
    spot_move_abs = abs(spot_move_pct) * entry_spot if entry_spot > 0 else 0
    delta_gain = d * spot_move_abs

    projected = entry_prem + delta_gain - theta_cost
    return max(projected, entry_prem * 0.05)  # floor at 5% of entry
