"""
core/option_buy_eval.py — is BUYING this option justified, given theta?

WHAT IT ANSWERS
---------------
Buying a call/put to ride momentum is a race between delta (which pays you
when the underlying moves your way) and theta (which charges you every day
regardless). The only question that matters before entry is:

    how far must the underlying move, per day, just to BREAK EVEN?

    required_daily_move = |theta_per_day| / delta        (in rupees)

Measured live on 2026-08-06 -- RELIANCE spot 1325, ATM 1320 CE, 19 DTE,
IV 20%, premium 30.55: theta -0.75/day against delta 0.571 means the stock
must move ~Rs 1.32/day (~0.10% of spot) EVERY day in your favour just to
stand still. Two flat days and roughly 5% of the premium is gone.

This module computes that from LIVE option-chain data -- real spot, real
premium, real IV, real listed expiry -- never from a model price, because the
whole point is to compare against what you would actually pay.

HONEST CONTEXT, DO NOT SKIP
---------------------------
This project has already measured option BUYING at PF 0.44 (see the swing-book
work), and hypothesis H-016 -- "the one requirement for option buying" -- was
tested and REJECTED. So this module is deliberately a GATE, not a generator:
it exists to reject buys whose required move exceeds what the underlying
actually does, which on the measured evidence is most of them. A tool that
made option buying look easy would be lying.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Dict, Optional


@dataclass
class BuyEvaluation:
    symbol: str
    option_type: str
    strike: float
    spot: float
    expiry: str
    dte: int
    premium: float
    iv_pct: float
    delta: float
    theta_per_day: float
    theta_pct_of_premium_per_day: float
    required_daily_move: float          # rupees on the UNDERLYING
    required_daily_move_pct: float      # as % of spot
    breakeven_underlying: float         # spot move needed to recover premium
    verdict: str
    reason: str

    def to_dict(self) -> Dict:
        return asdict(self)


def _atm_row(chain: list, spot: float) -> Optional[dict]:
    rows = [r for r in chain if r.get("strike")]
    return min(rows, key=lambda r: abs(float(r["strike"]) - spot)) if rows else None


def evaluate_buy(symbol: str, option_type: str = "CE",
                 strike: Optional[float] = None,
                 expected_daily_move_pct: Optional[float] = None,
                 chain: Optional[list] = None,
                 asof: Optional[date] = None) -> BuyEvaluation:
    """Evaluate buying one option leg using LIVE chain data.

    expected_daily_move_pct : the move you believe the momentum thesis
        delivers, as a % of spot per day. Supply the measured value, not a
        hope. If omitted the verdict reports the requirement only.
    """
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise ValueError("option_type must be CE or PE")

    if chain is None:
        from core.dashboard_data import get_option_chain
        chain = get_option_chain(symbol)
    if not chain:
        raise ValueError(f"no live option chain for {symbol}")

    spot = float(chain[0].get("_spot") or 0)
    expiry = str(chain[0].get("_expiry") or "")
    if spot <= 0 or not expiry:
        raise ValueError("chain missing spot/expiry — refusing to guess")

    row = (_atm_row(chain, spot) if strike is None
           else min(chain, key=lambda r: abs(float(r["strike"]) - strike)))
    if not row:
        raise ValueError("no usable strike in chain")

    k = float(row["strike"])
    prem = float(row.get(f"{option_type.lower()}_ltp") or 0)
    iv = float(row.get(f"{option_type.lower()}_iv") or 0)
    if prem <= 0 or iv <= 0:
        raise ValueError(f"no live premium/IV for {symbol} {k} {option_type}")

    today = asof or date.today()
    y, m, d = (int(x) for x in expiry.split("-"))
    dte = max((date(y, m, d) - today).days, 0)
    if dte == 0:
        return BuyEvaluation(
            symbol, option_type, k, spot, expiry, 0, prem, iv, 0.0, 0.0, 0.0,
            float("inf"), float("inf"), prem + k, "REJECT",
            "expiry day: all remaining premium is time value about to vanish")

    from core.options_greeks import BlackScholesModel
    g = BlackScholesModel().calculate_greeks(spot, k, dte / 365.0,
                                             iv / 100.0, option_type)
    delta = abs(float(g.delta))
    # Greeks convention here reports theta already per-day for short horizons;
    # normalise defensively so an annualised value cannot silently 365x.
    theta_day = float(g.theta)
    if abs(theta_day) > prem:          # an annual figure would dwarf premium
        theta_day /= 365.0
    theta_day = -abs(theta_day)

    theta_pct = abs(theta_day) / prem * 100.0
    req_move = abs(theta_day) / delta if delta > 1e-6 else float("inf")
    req_pct = req_move / spot * 100.0 if spot else float("inf")
    be = (k + prem) if option_type == "CE" else (k - prem)

    if expected_daily_move_pct is None:
        verdict = "REQUIREMENT ONLY"
        reason = (f"underlying must move {req_pct:.3f}%/day "
                  f"({req_move:.2f}) just to offset theta")
    elif expected_daily_move_pct >= req_pct:
        verdict = "PASS"
        reason = (f"expected {expected_daily_move_pct:.3f}%/day clears the "
                  f"{req_pct:.3f}%/day theta hurdle")
    else:
        verdict = "REJECT"
        reason = (f"expected {expected_daily_move_pct:.3f}%/day is BELOW the "
                  f"{req_pct:.3f}%/day theta hurdle — decay wins")

    return BuyEvaluation(
        symbol=symbol, option_type=option_type, strike=k, spot=spot,
        expiry=expiry, dte=dte, premium=round(prem, 2), iv_pct=round(iv, 2),
        delta=round(delta, 4), theta_per_day=round(theta_day, 4),
        theta_pct_of_premium_per_day=round(theta_pct, 3),
        required_daily_move=round(req_move, 3),
        required_daily_move_pct=round(req_pct, 4),
        breakeven_underlying=round(be, 2), verdict=verdict, reason=reason,
    )
