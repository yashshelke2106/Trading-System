"""
directional_buy.py — the correct way to buy options directionally, and the
requirement gate that says whether you are allowed to.

WHY BOTH HALVES MATTER
----------------------
Long-option mechanics are easy to get wrong in ways that guarantee losses even
WITH an edge (wrong strike, wrong DTE, holding into theta collapse, cutting
winners early). So this module encodes the correct mechanics — the "perfect
way" — as an explicit, checkable spec.

But mechanics cannot create edge. Buying a call is a bet on DIRECTION + SIZE +
SPEED simultaneously; the seller only needs "no big move". That asymmetry is
the volatility risk premium, and the buyer pays it. So the same module carries a
GATE: it refuses to emit a trade plan unless the preconditions that make buying
+EV are actually met.

This system currently FAILS the gate, measured:
  - directional accuracy 33.6% (below coin flip), n=1012
  - winning moves: median MFE 0.32%, 90th pct 1.54%, only 0.8% reach 3%
    -> the 3R/5R convexity that would rescue a low win rate DOES NOT OCCUR
So the gate returns BLOCKED here, and says exactly which requirement failed.
When/if a real directional edge exists, flip the same gate and the mechanics
below are already correct.

THE MECHANICS (the "perfect way"), and why each choice
-------------------------------------------------------
  STRIKE   slightly ITM (delta ~0.60-0.70), NOT far OTM.
           OTM lottery tickets have the worst expectancy: low delta means the
           spot must move hugely just to matter, and theta eats them fastest.
           ITM carries intrinsic value, so less of your premium is pure decay.
  DTE      30-45 days. Theta decay accelerates in the final ~21 days; buying
           weeklies maximises the decay you pay. Longer DTE buys time for the
           thesis to work.
  EXIT     time-stop BEFORE the theta cliff (exit by ~21 DTE regardless), plus
           a hard stop at ~50% of premium and a target that lets winners run.
  SIZE     small and fixed. Most long options expire worthless even with an
           edge; the distribution is lottery-shaped, so no single bet may
           matter. Never scale into a loser.
  IV       enter when IV is LOW relative to its own history. Buying high IV
           means paying up AND facing vol-crush if the move comes.

RUN
---
    python -m core.directional_buy --check          # requirement gate status
    python -m core.directional_buy --plan NIFTY --view up
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
from typing import Dict, List, Optional

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ssl._create_default_https_context = ssl._create_unverified_context

# ── Mechanics spec (the "perfect way") ──────────────────────────────────────
TARGET_DELTA = 0.65        # slightly ITM: intrinsic-heavy, less pure decay
DTE_MIN, DTE_MAX = 30, 45  # avoid the sub-21d theta cliff
EXIT_DTE = 21              # time-stop: out before decay accelerates
STOP_PREMIUM_FRAC = 0.50   # cut at -50% of premium paid
TARGET_R = 3.0             # let winners run to ~3R (needs the move to exist)
MAX_RISK_PER_TRADE = 0.02  # <=2% of book per bet; most expire worthless
IV_PERCENTILE_MAX = 40     # only buy when IV is in the cheaper 40% of its range

# ── Requirement gate thresholds (measured, not chosen) ──────────────────────
MEASURED_ACCURACY = 0.336        # journal spot win rate, n=1012
MEASURED_MFE_P90 = 1.54          # 90th pct favourable move, %
MOVE_NEEDED_FOR_3R = 3.0         # ~spot % to triple a 30d ATM at low IV
COIN_FLIP = 0.50


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_call(S, K, T, s, r=0.0):
    if T <= 0 or s <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + s * s / 2) * T) / (s * math.sqrt(T))
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d1 - s * math.sqrt(T))


def _bs_put(S, K, T, s, r=0.0):
    if T <= 0 or s <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + s * s / 2) * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _delta_call(S, K, T, s, r=0.0):
    if T <= 0 or s <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + s * s / 2) * T) / (s * math.sqrt(T))
    return _ncdf(d1)


def strike_for_delta(S: float, iv: float, dte: int, target_delta: float,
                     view: str = "up", step: int = 50) -> float:
    """Find the strike whose delta is closest to target (ITM side).

    For a call, higher delta = lower strike (deeper ITM). Searched on the real
    NSE strike grid (50-point steps for NIFTY) rather than a continuous value,
    because you can only trade listed strikes.
    """
    T = dte / 365.0
    best, best_err = S, 9e9
    lo, hi = int(S * 0.85 / step) * step, int(S * 1.15 / step) * step
    for k in range(lo, hi + step, step):
        d = _delta_call(S, k, T, iv)
        if view == "down":
            d = 1.0 - d                      # put delta magnitude
        err = abs(d - target_delta)
        if err < best_err:
            best, best_err = k, err
    return float(best)


# ── Requirement gate ────────────────────────────────────────────────────────

@dataclass
class Requirement:
    name: str
    needed: str
    actual: str
    passed: bool


@dataclass
class GateResult:
    allowed: bool
    requirements: List[Requirement] = field(default_factory=list)
    summary: str = ""

    def to_dict(self):
        d = asdict(self)
        return d


def check_requirements(accuracy: float = MEASURED_ACCURACY,
                       mfe_p90: float = MEASURED_MFE_P90,
                       iv_percentile: Optional[float] = None) -> GateResult:
    """The gate. Every requirement must pass before directional buying is +EV.

    Requirement 1 and 2 are the substantive ones: you must be right often
    enough, OR your winners must be big enough to carry a low hit rate. This
    system fails BOTH, which is why its long-option journal is PF 0.44.
    """
    reqs: List[Requirement] = []

    # 1. Directional accuracy. With 3R winners you can survive ~25%; but that
    #    requires requirement 2 to hold.
    reqs.append(Requirement(
        "Directional accuracy beats the payoff break-even",
        f">= 25% IF winners reach 3R (else >= 50%)",
        f"{accuracy*100:.1f}% (below coin flip {COIN_FLIP*100:.0f}%)",
        accuracy >= COIN_FLIP))

    # 2. Do winners actually RUN far enough to make 3R? This is the one that
    #    decides it, and it is measured, not assumed.
    reqs.append(Requirement(
        "Winning moves large enough for 3R option payoff",
        f"90th-pct favourable move >= {MOVE_NEEDED_FOR_3R:.0f}%",
        f"{mfe_p90:.2f}% (only 0.8% of trades reach 3%)",
        mfe_p90 >= MOVE_NEEDED_FOR_3R))

    # 3. Convex exit discipline — structural, adoptable.
    reqs.append(Requirement(
        "Convex exits: cut ~50% premium, ride winners to ~3R",
        "explicit stop + time-stop + runner target",
        "encoded in this module's spec (adoptable)",
        True))

    # 4. IV timing — adoptable, currently favourable.
    ivp = iv_percentile
    reqs.append(Requirement(
        "Enter at LOW IV (cheap premium, less vol-crush risk)",
        f"IV percentile <= {IV_PERCENTILE_MAX}",
        (f"{ivp:.0f}" if ivp is not None else "VIX ~12 = historically low"),
        True if ivp is None else ivp <= IV_PERCENTILE_MAX))

    # 5. Execution speed — theta runs while you click.
    reqs.append(Requirement(
        "Fast fills / fresh data (theta clock is running)",
        "sub-minute fills, live chain",
        "manual execution, ~3-min stale stock data",
        False))

    allowed = all(r.passed for r in reqs)
    failed = [r.name for r in reqs if not r.passed]
    summary = ("ALLOWED — preconditions met" if allowed else
               "BLOCKED — failing: " + "; ".join(failed))
    return GateResult(allowed=allowed, requirements=reqs, summary=summary)


# ── Trade plan (only emitted when the gate allows) ──────────────────────────

@dataclass
class TradePlan:
    symbol: str
    view: str
    spot: float
    iv: float
    dte: int
    strike: float
    option_type: str
    delta: float
    premium: float
    stop_premium: float
    target_premium: float
    exit_by_dte: int
    max_risk_pct: float
    note: str

    def to_dict(self):
        return asdict(self)


def _live_spot_vix() -> Optional[tuple]:
    try:
        from core.live_quotes import get_index_quotes
        q = get_index_quotes()
        s = q.get("NIFTY")
        v = q.get("INDIAVIX")
        if s and v:
            return float(s.last), float(v.last)
    except Exception:
        pass
    return None


def build_plan(symbol: str = "NIFTY", view: str = "up",
               dte: int = 35) -> Optional[TradePlan]:
    """The mechanically-correct plan. Returns None if live data unavailable."""
    live = _live_spot_vix()
    if not live:
        return None
    S, vix = live
    iv = vix / 100.0
    dte = max(DTE_MIN, min(dte, DTE_MAX))
    K = strike_for_delta(S, iv, dte, TARGET_DELTA, view)
    T = dte / 365.0
    if view == "up":
        prem = _bs_call(S, K, T, iv)
        delta = _delta_call(S, K, T, iv)
        otype = "CALL"
    else:
        prem = _bs_put(S, K, T, iv)
        delta = 1.0 - _delta_call(S, K, T, iv)
        otype = "PUT"
    return TradePlan(
        symbol=symbol, view=view, spot=round(S, 2), iv=round(vix, 2), dte=dte,
        strike=K, option_type=otype, delta=round(delta, 3),
        premium=round(prem, 2),
        stop_premium=round(prem * (1 - STOP_PREMIUM_FRAC), 2),
        target_premium=round(prem * (1 + TARGET_R), 2),
        exit_by_dte=EXIT_DTE, max_risk_pct=MAX_RISK_PER_TRADE * 100,
        note=("Mechanically correct spec. Emitted for reference ONLY — the "
              "requirement gate must pass before this is +EV."))


# ── Reporting ───────────────────────────────────────────────────────────────

def _print_gate(g: GateResult) -> None:
    print("\n=== REQUIREMENT GATE: directional option buying ===\n")
    for i, r in enumerate(g.requirements, 1):
        mark = "PASS" if r.passed else "FAIL"
        print(f"{i}. [{mark}] {r.name}")
        print(f"        needed: {r.needed}")
        print(f"        actual: {r.actual}")
    print(f"\nVERDICT: {g.summary}\n")


def _print_plan(p: Optional[TradePlan]) -> None:
    if p is None:
        print("  (live spot/IV unavailable — cannot build a plan)")
        return
    print("=== MECHANICALLY CORRECT PLAN (the 'perfect way') ===\n")
    print(f"  {p.symbol} {p.view.upper()}  spot {p.spot:,.0f}  IV {p.iv}%")
    print(f"  BUY  {p.strike:,.0f} {p.option_type}   delta {p.delta}  "
          f"({p.dte} DTE)")
    print(f"  premium      ~{p.premium:,.2f}")
    print(f"  hard stop    {p.stop_premium:,.2f}   (-{STOP_PREMIUM_FRAC:.0%} of premium)")
    print(f"  runner target {p.target_premium:,.2f}   (+{TARGET_R:.0f}R)")
    print(f"  TIME STOP    exit by {p.exit_by_dte} DTE regardless — theta cliff")
    print(f"  size         <= {p.max_risk_pct:.0f}% of book on this single bet")
    print(f"\n  WHY these choices:")
    print(f"    delta ~{TARGET_DELTA}: slightly ITM. Far-OTM 'cheap' options have the")
    print(f"      worst expectancy — low delta + fastest decay.")
    print(f"    {DTE_MIN}-{DTE_MAX} DTE: theta accelerates under ~21 days. Weeklies")
    print(f"      maximise the decay you PAY.")
    print(f"    time stop: most long-option losses are decay, not direction.")
    print(f"    small size: the payoff is lottery-shaped; no bet may matter.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Directional option buying: gate + correct mechanics.")
    ap.add_argument("--check", action="store_true", help="requirement gate only")
    ap.add_argument("--plan", metavar="SYMBOL", nargs="?", const="NIFTY")
    ap.add_argument("--view", default="up", choices=["up", "down"])
    ap.add_argument("--dte", type=int, default=35)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    gate = check_requirements()
    if args.json:
        out = {"gate": gate.to_dict()}
        if args.plan:
            p = build_plan(args.plan, args.view, args.dte)
            out["plan"] = p.to_dict() if p else None
        print(json.dumps(out, indent=2, default=str))
        return 0

    _print_gate(gate)
    if args.plan:
        _print_plan(build_plan(args.plan, args.view, args.dte))
        if not gate.allowed:
            print("  ^ The plan above is CORRECT MECHANICS on a FAILED gate.")
            print("    Trading it is -EV: you would be executing well on a bet")
            print("    the evidence says loses. Fix the gate first.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
