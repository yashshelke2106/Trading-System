"""
swing_book.py — the 50/50 equity + options swing book.

WHAT THIS IS
------------
A two-sleeve swing book sized for a specific account, built from the only two
return sources this programme has evidence for:

  EQUITY  50%   index core with a 200-DMA trend overlay (core.allocation).
                Harvests the EQUITY risk premium. Validated: the exact-edge
                study (docs/research/THE_EXACT_EDGE_REQUIRED.md) showed the
                only reliable edge at swing horizons is beta, and a diversified
                index holding is the right vehicle for it.

  OPTIONS 50%   DEFINED-RISK SHORT PREMIUM (NIFTY iron condor). Harvests the
                VOLATILITY risk premium. Sized so the maximum loss is capped
                and known before entry.

WHY SHORT PREMIUM AND NOT LONG
------------------------------
This system's own journal measured option BUYING at profit factor 0.44, win
rate 33.6%, expectancy −0.54% per trade over 1,012 clean trades. Allocating
50% of a book to that is not a strategy, it is a leak. Selling defined-risk
premium is the other side of that trade: it has a named counterparty (buyers
overpaying for protection), it wins ~80% of the time by construction, and its
tail is capped by the protective wings rather than open-ended.

WHAT THE EVIDENCE ACTUALLY SUPPORTS (read before sizing)
--------------------------------------------------------
The condor gate (H-014, 132 monthly trades, 2010-2026, net of costs) returned:
  mean +4.4% of capital-at-risk per trade, win rate 81%
  in-sample  +5.3%  t = +1.82
  OUT-OF-SAMPLE +2.2%  t = +0.42   <-- NOT distinguishable from zero
So the options sleeve is a STRUCTURALLY sound, tail-capped way to express a
thin and statistically unproven premium. It is not a validated edge. It is
sized here as a small, defined-risk, forward-test position — never as the
engine of the book.

THE BINDING CONSTRAINT AT SMALL ACCOUNTS IS CAPITAL, NOT EDGE
-------------------------------------------------------------
NIFTY's lot size is 75. One iron condor's margin is roughly
(wing_width − net_credit) × 75, so even a narrow 100-point wing ties up
~Rs 5,250. On a Rs 15,000 book the options sleeve is Rs 7,500, which supports
exactly ONE narrow condor — a single undiversified position holding ~70% of
the sleeve. This module computes and REPORTS that concentration rather than
hiding it, and refuses to propose a position it cannot actually fund.

RUN
---
    python -m core.swing_book --plan --capital 15000
    python -m core.swing_book --plan --capital 500000
"""
from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import warnings
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ssl._create_default_https_context = ssl._create_unverified_context

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK_PATH = os.path.join(_ROOT, "logs", "swing_book.json")

# ── Structural constants ────────────────────────────────────────────────────
NIFTY_LOT = 75                 # NIFTY option lot size
EQUITY_FRAC = 0.50
OPTIONS_FRAC = 0.50
# Wing widths to consider, narrowest first — the narrowest fundable one wins,
# because a wider wing on a small account means no position at all.
WING_CANDIDATES = [100, 200, 300, 500]
# LAST-RESORT fallback only. A flat credit/wing ratio is WRONG: the true ratio
# falls as the wing widens (measured at NIFTY 24,261 / VIX 12.1: 29.0% at
# 100pt, 26.4% at 200pt, 20.1% at 500pt) and it also moves with the vol regime.
# Using a flat 0.30 UNDERSTATES max loss on wide wings by ~14% — understating
# risk is the dangerous direction, so the planner prices the condor properly
# with Black-Scholes off live NIFTY + India VIX and only falls back to this
# constant when that data is unreachable (and says so when it does).
CREDIT_FRAC_FALLBACK = 0.30
CONDOR_DTE = 30                # days to expiry the sleeve targets
SHORT_STRIKE_SD = 1.0          # short legs ~1 SD OTM (~16 delta)
# Refuse any single position that would risk more than this share of its sleeve.
MAX_SLEEVE_CONCENTRATION = 0.80


@dataclass
class Sleeve:
    name: str
    target_frac: float
    capital: float
    fundable: bool
    detail: str
    positions: List[Dict] = field(default_factory=list)
    warnings_: List[str] = field(default_factory=list)


@dataclass
class BookPlan:
    capital: float
    asof: str
    equity: Dict
    options: Dict
    warnings_: List[str]
    note: str

    def to_dict(self) -> Dict:
        return {"capital": self.capital, "asof": self.asof,
                "equity": self.equity, "options": self.options,
                "warnings": self.warnings_, "note": self.note}


# ── Equity sleeve ───────────────────────────────────────────────────────────

def _nifty_bees_price() -> Optional[float]:
    try:
        import yfinance as yf
        d = yf.Ticker("NIFTYBEES.NS").history(period="5d", interval="1d")
        if d is not None and len(d):
            return float(d["Close"].iloc[-1])
    except Exception:
        pass
    return None


def plan_equity(capital: float) -> Dict:
    """Index core sized by the live 200-DMA overlay state."""
    sleeve_cap = capital * EQUITY_FRAC
    out: Dict = {"target_frac": EQUITY_FRAC, "sleeve_capital": round(sleeve_cap, 2)}

    state, eq_w, note = "unknown", 1.0, "overlay unavailable — defaulting to core"
    try:
        from allocation_task import build_status
        st = build_status(refresh=False)
        ov = (st.get("targets") or {}).get("overlay") or {}
        if ov:
            state = ov.get("state", "unknown")
            eq_w = float(ov.get("equity_weight", 1.0))
            note = ov.get("note", "")
        out["nifty"] = st.get("nifty")
        out["ma200"] = st.get("ma200")
        out["distance_to_flip_pct"] = st.get("distance_to_flip_pct")
    except Exception as exc:
        out["error"] = str(exc)

    px = _nifty_bees_price()
    equity_rupees = sleeve_cap * eq_w
    out.update({"instrument": "NIFTYBEES", "overlay_state": state,
                "overlay_equity_weight": eq_w, "note": note,
                "price": round(px, 2) if px else None})
    if px:
        units = int(equity_rupees // px)
        deployed = units * px
        out.update({"units": units, "deployed": round(deployed, 2),
                    "cash_in_sleeve": round(sleeve_cap - deployed, 2)})
        if units == 0:
            out["fundable"] = False
            out["reason"] = (f"Rs{equity_rupees:,.0f} buys 0 units at "
                             f"Rs{px:,.2f} — sleeve too small")
        else:
            out["fundable"] = True
    else:
        out["fundable"] = False
        out["reason"] = "price unavailable"
    return out


# ── Options sleeve ──────────────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call(S: float, K: float, T: float, s: float, r: float = 0.0) -> float:
    if T <= 0 or s <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + s * s / 2) * T) / (s * math.sqrt(T))
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d1 - s * math.sqrt(T))


def _bs_put(S: float, K: float, T: float, s: float, r: float = 0.0) -> float:
    if T <= 0 or s <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + s * s / 2) * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _live_nifty_vix() -> Optional[tuple]:
    """(NIFTY spot, India VIX). None when unreachable."""
    try:
        import yfinance as yf
        s = yf.Ticker("^NSEI").history(period="5d", interval="1d")
        v = yf.Ticker("^INDIAVIX").history(period="5d", interval="1d")
        if len(s) and len(v):
            return float(s["Close"].iloc[-1]), float(v["Close"].iloc[-1])
    except Exception:
        pass
    return None


def condor_credit_points(wing: float, spot: float, vix: float,
                         dte: int = CONDOR_DTE,
                         sd_mult: float = SHORT_STRIKE_SD) -> float:
    """Net credit (index points) for a ~1SD iron condor with `wing`-wide wings.

    Priced properly rather than assumed: the credit/wing ratio is NOT constant
    — it falls as the wing widens and rises with implied vol.
    """
    iv = vix / 100.0
    T = dte / 365.0
    sigma_pts = spot * iv * math.sqrt(T)
    kc, kp = spot + sd_mult * sigma_pts, spot - sd_mult * sigma_pts
    short_prem = _bs_call(spot, kc, T, iv) + _bs_put(spot, kp, T, iv)
    long_prem = _bs_call(spot, kc + wing, T, iv) + _bs_put(spot, kp - wing, T, iv)
    return max(short_prem - long_prem, 0.0)


def plan_options(capital: float, lot: int = NIFTY_LOT) -> Dict:
    """Defined-risk short-premium sleeve, sized to what is actually fundable."""
    sleeve_cap = capital * OPTIONS_FRAC
    out: Dict = {"target_frac": OPTIONS_FRAC,
                 "sleeve_capital": round(sleeve_cap, 2),
                 "structure": "NIFTY iron condor (~1SD short strikes + wings)",
                 "lot_size": lot, "dte": CONDOR_DTE}

    live = _live_nifty_vix()
    if live:
        spot, vix = live
        out.update({"spot": round(spot, 2), "vix": round(vix, 2),
                    "pricing": "black-scholes on live NIFTY + India VIX"})
    else:
        spot = vix = None
        out["pricing"] = (f"FALLBACK flat credit/wing={CREDIT_FRAC_FALLBACK} "
                          f"— live NIFTY/VIX unavailable; max loss on wide "
                          f"wings may be UNDERSTATED")

    options = []
    for wing in WING_CANDIDATES:
        if spot and vix:
            credit = condor_credit_points(wing, spot, vix)
        else:
            credit = wing * CREDIT_FRAC_FALLBACK
        max_loss = (wing - credit) * lot          # capital at risk per condor
        n = int(sleeve_cap // max_loss) if max_loss > 0 else 0
        options.append({"wing_points": wing,
                        "credit_points": round(credit, 1),
                        "credit_frac_of_wing": round(credit / wing, 3),
                        "max_loss_per_lot": round(max_loss, 2),
                        "lots_affordable": n})
    out["ladder"] = options

    fundable = [o for o in options if o["lots_affordable"] >= 1]
    if not fundable:
        cheapest = options[0]
        out.update({
            "fundable": False,
            "reason": (f"cheapest structure ({cheapest['wing_points']}pt wing) "
                       f"risks Rs{cheapest['max_loss_per_lot']:,.0f} per lot, "
                       f"more than the Rs{sleeve_cap:,.0f} sleeve"),
            "capital_needed_for_one_lot": round(cheapest["max_loss_per_lot"] /
                                                OPTIONS_FRAC, 2),
        })
        return out

    # Narrowest fundable wing, so the sleeve can hold a position at all.
    pick = fundable[0]
    at_risk = pick["max_loss_per_lot"] * pick["lots_affordable"]
    concentration = at_risk / sleeve_cap if sleeve_cap else 1.0
    out.update({
        "fundable": True,
        "chosen": pick,
        "lots": pick["lots_affordable"],
        "capital_at_risk": round(at_risk, 2),
        "sleeve_concentration": round(concentration, 3),
        "expected_credit": round(pick["credit_points"] * lot *
                                 pick["lots_affordable"], 2),
    })
    warns = []
    if concentration > MAX_SLEEVE_CONCENTRATION:
        warns.append(
            f"single position holds {concentration:.0%} of the options sleeve "
            f"— no diversification; one bad month is the whole sleeve")
    if pick["lots_affordable"] == 1:
        warns.append("only ONE lot affordable: outcomes are lumpy and the "
                     "measured +4.4%/trade average is meaningless at n=1/month")
    out["warnings"] = warns
    return out


# ── Book ────────────────────────────────────────────────────────────────────

def plan(capital: float) -> BookPlan:
    eq = plan_equity(capital)
    op = plan_options(capital)

    warns: List[str] = []
    warns.extend(op.get("warnings", []))
    if not op.get("fundable"):
        need = op.get("capital_needed_for_one_lot")
        warns.append(
            f"OPTIONS SLEEVE NOT FUNDABLE at Rs{capital:,.0f}. "
            + (f"A 50% options sleeve needs about Rs{need:,.0f} total capital "
               f"to hold one narrow condor." if need else ""))
        warns.append("Until then the book is equity-only; do not substitute "
                     "BOUGHT options (measured PF 0.44) to fill the gap.")
    if not eq.get("fundable"):
        warns.append("EQUITY SLEEVE NOT FUNDABLE: " + str(eq.get("reason", "")))

    note = (
        "Equity sleeve harvests the equity risk premium (validated). Options "
        "sleeve harvests the volatility risk premium via DEFINED-RISK short "
        "premium — real but THIN and statistically unproven out-of-sample "
        "(H-014: OOS +2.2%/trade, t=+0.42). Size it as a forward test, not as "
        "the engine of the book. PAPER_TRADE stays True."
    )
    return BookPlan(capital=capital,
                    asof=datetime.now(timezone.utc).date().isoformat(),
                    equity=eq, options=op, warnings_=warns, note=note)


def save(p: BookPlan) -> None:
    os.makedirs(os.path.dirname(BOOK_PATH), exist_ok=True)
    with open(BOOK_PATH, "w", encoding="utf-8") as fh:
        json.dump(p.to_dict(), fh, indent=2, default=str)


def _print(p: BookPlan) -> None:
    print(f"\n=== 50/50 SWING BOOK — Rs{p.capital:,.0f} ({p.asof}) ===\n")

    e = p.equity
    print(f"EQUITY SLEEVE  {e['target_frac']:.0%}  = Rs{e['sleeve_capital']:,.0f}")
    print(f"  overlay state : {e.get('overlay_state')}  "
          f"(equity weight {e.get('overlay_equity_weight')})")
    if e.get("nifty"):
        print(f"  NIFTY {e['nifty']:,.0f} vs 200DMA {e.get('ma200'):,.0f}  "
              f"({e.get('distance_to_flip_pct')}% to flip)")
    if e.get("fundable"):
        print(f"  -> BUY {e['units']} NIFTYBEES @ Rs{e['price']:,.2f} "
              f"= Rs{e['deployed']:,.2f}   cash Rs{e['cash_in_sleeve']:,.2f}")
    else:
        print(f"  -> NOT FUNDABLE: {e.get('reason')}")

    o = p.options
    print(f"\nOPTIONS SLEEVE {o['target_frac']:.0%}  = Rs{o['sleeve_capital']:,.0f}")
    print(f"  structure     : {o['structure']}  (lot {o['lot_size']}, "
          f"{o.get('dte')}d)")
    if o.get("spot"):
        print(f"  priced on     : NIFTY {o['spot']:,.0f}, VIX {o['vix']:.2f} "
              f"[{o['pricing']}]")
    else:
        print(f"  pricing       : {o['pricing']}")
    print(f"  {'wing':>6}{'credit':>9}{'cr/wing':>9}{'max loss/lot':>15}{'lots':>7}")
    for row in o["ladder"]:
        print(f"  {row['wing_points']:>6}{row['credit_points']:>9.1f}"
              f"{row.get('credit_frac_of_wing', 0):>8.1%}"
              f"{row['max_loss_per_lot']:>15,.0f}{row['lots_affordable']:>7}")
    if o.get("fundable"):
        c = o["chosen"]
        print(f"  -> SELL {o['lots']} condor(s), {c['wing_points']}pt wings")
        print(f"     capital at risk Rs{o['capital_at_risk']:,.0f} "
              f"({o['sleeve_concentration']:.0%} of sleeve), "
              f"credit ~Rs{o['expected_credit']:,.0f}")
    else:
        print(f"  -> NOT FUNDABLE: {o.get('reason')}")

    if p.warnings_:
        print("\nWARNINGS")
        for w in p.warnings_:
            print(f"  ! {w}")
    print(f"\n{p.note}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="50/50 equity + options swing book.")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--capital", type=float, default=15000.0)
    args = ap.parse_args()
    if args.plan:
        p = plan(args.capital)
        save(p)
        _print(p)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
