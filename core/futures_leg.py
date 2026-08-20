"""
Futures-leg enrichment — express a directional signal through the stock
FUTURE instead of an option.

WHY (from the 16-day / 911-trade live journal):
  - On SL_HIT the underlying moved only -0.60% (median) but the OPTION
    premium lost -9.56%. Stops fired on theta + IV crush + spread, not price.
  - 15% of trades had the direction RIGHT (+1.79% spot) yet the option still
    lost (-12.95%). Pure instrument tax.
  - The backtest edge (PF 1.17) was computed on SPOT. Futures (delta ~1, no
    theta decay, tight spread on liquid names) express that edge directly.

This module attaches a futures leg to a signal dict: lot size, futures
entry/SL/target (= spot levels, basis ignored — negligible for swing on
liquid names), and quantity for 1 lot. It strips option-specific fields so
downstream code doesn't mistake it for an option trade.

No option chain, no strike selection, no theta/IV gates. Same expiry-day
caution applies (don't enter a future on its last trading day — roll risk),
but there is no premium-to-zero gamma trap.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

try:
    import config
except Exception:                       # pragma: no cover - config is untracked
    config = None                       # lot_size_for still answers via scrip_master

log = logging.getLogger(__name__)

# Option-only keys we remove so a futures signal never looks like an option trade.
_OPTION_KEYS = (
    "option_strike", "option_expiry", "option_type",
    "entry_prem", "sl_prem", "target_prem", "delta", "iv_pct",
    "prem_source", "spread_pct", "bid", "ask", "theta", "theta_penalty",
)


def lot_size_for(symbol: str) -> int:
    """NSE F&O lot size — LIVE scrip master first (config.NSE_LOT_SIZES goes
    stale when NSE revises lots), then the static map, then 1 so quantity math
    never breaks."""
    sym = str(symbol or "").upper()
    try:
        from core.scrip_master import lot_size as _live_lot
        live = _live_lot(sym)
        if live and live > 0:
            return int(live)
    except Exception:
        pass
    # Static fallback only. Measured 2026-08-20 against the live journal: just
    # 3 of 70 traded symbols are correct here - 51 are absent (and would size
    # at ONE SHARE) and 16 are wrong by up to 6x. Never read this map directly;
    # always come through this function.
    return max(1, int(getattr(config, "NSE_LOT_SIZES", {}).get(sym, 1)))


def attach_futures_leg(s: Dict) -> Optional[Dict]:
    """Attach a stock-futures leg to signal `s` (mutated + returned).

    Returns None if the symbol has no usable spot levels. Keeps the signal's
    existing entry_price / sl_price / target_price as the FUTURES levels
    (delta ~1; basis ignored for liquid swing names).
    """
    try:
        sym = s.get("symbol")
        entry = float(s.get("entry_price") or 0)
        sl = float(s.get("sl_price") or 0)
        target = float(s.get("target_price") or 0)
        if not sym or entry <= 0 or sl <= 0 or target <= 0:
            return None

        lot = lot_size_for(sym)
        risk_per_unit = abs(entry - sl)
        reward_per_unit = abs(target - entry)
        rr = (reward_per_unit / risk_per_unit) if risk_per_unit > 0 else 0.0

        # Strip option-only fields
        for k in _OPTION_KEYS:
            s.pop(k, None)

        s.update({
            "instrument": "FUT",
            "lot_size": lot,
            "quantity": lot,                      # 1 lot default (sizing layer can scale)
            "fut_entry": round(entry, 2),
            "fut_sl": round(sl, 2),
            "fut_target": round(target, 2),
            "rr_ratio": round(rr, 2),
            # notional + rupee risk for the risk/sizing layer
            "notional": round(entry * lot, 2),
            "risk_rupees": round(risk_per_unit * lot, 2),
        })
        s["reason"] = f'{s.get("reason", "")} | FUT x{lot} RR={rr:.1f}'
        return s
    except Exception as e:
        log.debug(f"[FUT] attach_futures_leg failed for {s.get('symbol')}: {e}")
        return None
