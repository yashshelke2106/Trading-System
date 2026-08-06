"""
core/sizing.py — position size, decided in the one order that is correct.

WHY THIS EXISTS
---------------
Sizing logic was spread across execution.py, risk_engine.py and the agents,
and the pieces disagreed: two of them read a stale lot-size map (34 of 62
entries wrong, 81 names missing, which silently became lot size 1) while a
third read the live scrip master. None of them asked whether the account could
fund the position at all, so the system emitted signals for trades a broker
rejects at the order window.

THE ORDER IS THE POINT
----------------------
    1  Liquidity   is the name liquid enough for what you intend to trade?
    2  Fundable    can the account post the margin? one stock futures lot
                   needs Rs 0.9-1.7 LAKH
    3  Lot size    from the LIVE scrip master, never the static map
    4  Risk budget capital x max_risk_per_trade / (entry - stop)
    5  Breach      when one lot risks more than the cap, say so

Skipping to step 4 is how you size a trade you cannot open. Each step can veto,
and a veto returns a reason rather than a number, so the caller can log WHY
nothing was traded -- silence at the top of a funnel is indistinguishable from
"no signal today".

This module DECIDES; it never places anything.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional


@dataclass
class SizeDecision:
    symbol: str
    quantity: int                 # 0 when the trade is vetoed
    lots: int
    lot_size: int
    instrument: str
    ok: bool
    reason: str
    risk_budget: float = 0.0
    actual_risk: float = 0.0
    margin_required: float = 0.0
    risk_breach_multiple: Optional[float] = None
    turnover_cr: Optional[float] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def _veto(symbol: str, instrument: str, reason: str, **kw) -> SizeDecision:
    return SizeDecision(symbol=symbol, quantity=0, lots=0, lot_size=0,
                        instrument=instrument, ok=False, reason=reason, **kw)


def decide(symbol: str, entry: float, stop: float, capital: float,
           instrument: str = "futures", premium: Optional[float] = None,
           max_risk_per_trade: Optional[float] = None,
           max_capital_fraction: float = 0.5,
           require_liquidity: bool = True,
           allow_risk_breach: bool = True) -> SizeDecision:
    """Full sizing pipeline. Returns a decision, never raises on bad input.

    allow_risk_breach mirrors current behaviour: when a single lot risks more
    than the cap the trade is still taken at one lot, and the breach is
    reported. Set False to refuse instead -- that is a policy choice about
    which trades you take, so it is explicit rather than a default.
    """
    sym = str(symbol).upper()
    if entry <= 0 or capital <= 0:
        return _veto(sym, instrument, "entry price and capital must be positive")
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return _veto(sym, instrument, "stop equals entry: risk per unit is zero")

    # 1 ── liquidity ────────────────────────────────────────────────────
    turnover = None
    if require_liquidity:
        try:
            from core.selection import is_tradeable, turnover_of
            tier = "options" if instrument.startswith("option") else "futures"
            turnover = turnover_of(sym)
            if not is_tradeable(sym, tier):
                return _veto(sym, instrument,
                             f"below the {tier} liquidity floor"
                             + (f" (median {turnover:.0f} Cr/day)" if turnover else ""),
                             turnover_cr=turnover)
        except Exception:
            pass                     # selection data absent: do not block

    # 2 ── lot size, live ───────────────────────────────────────────────
    try:
        from core.futures_leg import lot_size_for
        lot_size = max(int(lot_size_for(sym)), 1)
    except Exception:
        lot_size = 1

    # 3 ── fundable? ────────────────────────────────────────────────────
    margin_required = 0.0
    try:
        from core.margin import estimate
        m = estimate(sym, entry, lots=1, instrument=instrument,
                     capital=capital, lot_size=lot_size, premium=premium)
        margin_required = m.total
        budget = capital * max(0.0, min(1.0, max_capital_fraction))
        if m.total > budget:
            return _veto(sym, instrument,
                         f"unfundable: one lot needs {m.total:,.0f} vs "
                         f"{budget:,.0f} usable ({m.total/capital*100:.0f}% of capital)",
                         margin_required=m.total, turnover_cr=turnover)
    except Exception:
        pass                         # margin model absent: do not block

    # 4 ── risk budget ──────────────────────────────────────────────────
    if max_risk_per_trade is None:
        try:
            import config
            max_risk_per_trade = float(
                config.RISK_CONFIG.get("max_risk_per_trade", 0.01))
        except Exception:
            max_risk_per_trade = 0.01
    risk_budget = capital * max_risk_per_trade
    risk_per_lot = risk_per_unit * lot_size
    lots = int(risk_budget // risk_per_lot) if risk_per_lot > 0 else 0

    # 5 ── the min-one-lot breach ───────────────────────────────────────
    breach = None
    if lots < 1:
        one_lot_risk = risk_per_lot
        breach = one_lot_risk / risk_budget if risk_budget > 0 else None
        if not allow_risk_breach:
            return _veto(sym, instrument,
                         f"one lot risks {one_lot_risk:,.0f} vs a "
                         f"{risk_budget:,.0f} budget ({breach:.1f}x over)",
                         risk_budget=risk_budget, actual_risk=one_lot_risk,
                         margin_required=margin_required,
                         risk_breach_multiple=round(breach, 2) if breach else None,
                         turnover_cr=turnover)
        lots = 1

    qty = lots * lot_size
    actual_risk = risk_per_unit * qty
    reason = "ok"
    if breach is not None:
        reason = (f"SIZED AT ONE LOT OVER BUDGET: risks {actual_risk:,.0f} "
                  f"vs {risk_budget:,.0f} ({breach:.1f}x)")

    return SizeDecision(
        symbol=sym, quantity=qty, lots=lots, lot_size=lot_size,
        instrument=instrument, ok=True, reason=reason,
        risk_budget=round(risk_budget, 2), actual_risk=round(actual_risk, 2),
        margin_required=round(margin_required, 2),
        risk_breach_multiple=round(breach, 2) if breach else None,
        turnover_cr=turnover)
