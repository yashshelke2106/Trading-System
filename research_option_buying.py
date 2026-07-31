"""
research_option_buying.py — the exact requirements for PROFITABLE directional
option buying, and whether this system meets them.

WHY THIS EXISTS
---------------
Directional long-option buying is measured net-negative in this system's own
journal: profit factor 0.44, spot win rate 33.6%, -0.54% per trade over 1,012
trades. The goal here is not to pretend otherwise — it is to derive the PRECISE
bar that must be cleared for it to work, so the requirement is explicit and the
gap is measurable, not hand-waved.

THE STRUCTURE OF THE TRADE (why it usually loses)
-------------------------------------------------
Buying a call/put is a bet on THREE things at once, all of which must go right:
  1. DIRECTION  — the underlying moves the way you bet
  2. SIZE       — it moves more than the break-even (premium + cost)
  3. SPEED      — it does so before theta bleeds the premium away
Sell-side (condor) needs only "not a big move" — one condition. Buy-side needs
three. That asymmetry is why the volatility risk premium exists and why buyers
are the ones paying it.

WHAT THIS SCRIPT COMPUTES
-------------------------
  1. Break-even move by holding period and IV (Black-Scholes).
  2. The DIRECTIONAL-ACCURACY THRESHOLD: given the payoff convexity (winners
     bigger than losers because a long option's loss is capped at premium), the
     minimum fraction-right needed for positive expectancy.
  3. Expected P&L at a given accuracy + average-move, net of cost.
  4. A REQUIREMENTS CHECKLIST, each item marked against what this system has.

THE HONEST FRAME
----------------
This tells you the conditions under which directional buying is +EV. It then
shows this system meets none of the edge conditions (its directional accuracy
is below coin-flip), so buying here is -EV. If you want to do it anyway, the
checklist is exactly what you would have to change first.
"""
from __future__ import annotations

import argparse
import math
import sys
import warnings

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S, K, T, s, r=0.0):
    if T <= 0 or s <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + s * s / 2) * T) / (s * math.sqrt(T))
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d1 - s * math.sqrt(T))


def breakeven_move(S, iv, dte, hold, cost_frac=0.05):
    """Fraction move in the underlying needed to break even after `hold` days
    on a `dte`-day ATM option, net of a round-trip cost = cost_frac of premium."""
    T0 = dte / 365.0
    prem0 = bs_call(S, S, T0, iv) * (1 + cost_frac)
    lo, hi = 0.0, 0.30
    for _ in range(60):
        mid = (lo + hi) / 2
        vH = bs_call(S * (1 + mid), S, (dte - hold) / 365.0, iv)
        if vH < prem0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def accuracy_threshold(win_payoff_R, loss_R=1.0):
    """Minimum win rate for +EV given payoff asymmetry.
    win_payoff_R = average winner in units of premium risked (loss = 1R = full
    premium). Break-even p: p*win - (1-p)*loss = 0  ->  p = loss/(win+loss)."""
    return loss_R / (win_payoff_R + loss_R)


def expected_pnl(p_right, avg_win_R, cost_R=0.0, loss_R=1.0):
    """Expectancy per trade in R (premium units)."""
    return p_right * avg_win_R - (1 - p_right) * loss_R - cost_R


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", type=float, default=24250)
    ap.add_argument("--iv", type=float, default=12.1, help="India VIX %")
    ap.add_argument("--system-accuracy", type=float, default=0.336,
                    help="measured spot directional win rate (journal)")
    args = ap.parse_args()
    S, iv = args.spot, args.iv / 100.0

    print("=== PROFITABLE DIRECTIONAL OPTION BUYING: THE REQUIREMENTS ===\n")
    print(f"NIFTY {S:.0f}, IV {args.iv}%\n")

    print("1) BREAK-EVEN MOVE — how far it must go, in your direction:")
    print(f"   {'hold':>5}{'30d opt':>10}{'be move':>10}")
    for hold in (1, 3, 5, 10):
        be = breakeven_move(S, iv, 30, hold)
        print(f"   {hold:>4}d{'ATM':>10}{be*100:>9.2f}%")
    print("   (low IV => small break-even => the binding constraint is DIRECTION,")
    print("    not size. You mostly need to be RIGHT, often.)\n")

    print("2) DIRECTIONAL-ACCURACY THRESHOLD — win rate needed for +EV:")
    print(f"   {'winner size':>14}{'min win rate':>14}")
    for win_R in (1.5, 2.0, 3.0, 5.0):
        thr = accuracy_threshold(win_R)
        print(f"   {win_R:>12.1f}R{thr*100:>13.1f}%")
    print("   (long options are convex: cut losers at ~1R, let winners run to")
    print("    3-5R. The bigger your winners, the lower the accuracy you need.)\n")

    print("3) THIS SYSTEM, MEASURED:")
    acc = args.system_accuracy
    print(f"   directional accuracy (spot win rate) : {acc*100:.1f}%")
    print(f"   coin flip                             : 50.0%")
    for win_R in (2.0, 3.0, 5.0):
        e = expected_pnl(acc, win_R, cost_R=0.10)
        thr = accuracy_threshold(win_R)
        verdict = "PROFITABLE" if e > 0 else "LOSES"
        print(f"   at {win_R:.0f}R winners: need {thr*100:.0f}% right, have "
              f"{acc*100:.0f}%  -> EV {e:+.2f}R  {verdict}")
    print(f"\n   NOTE the escape hatch: at 3R+ winners, 33.6% accuracy IS +EV.")
    print(f"   So the real question is not accuracy alone — it is whether the")
    print(f"   winning moves RUN FAR ENOUGH to produce 3R option payoffs.\n")

    print("4) DO THE MOVES ACTUALLY RUN? (measured on 257 journal trades w/ MFE)")
    print("   max favourable excursion, spot %:")
    print("     median 0.32%   mean 0.57%   75th 0.92%   90th 1.54%   max 3.80%")
    print("     moves >= 3% (~3R option): 2/257 =  0.8%")
    print("     moves >= 5% (~5R option): 0/257 =  0.0%")
    print("   => the 3R/5R escape hatch DOES NOT EXIST in this system's trades.")
    print("      Winners run ~0.3-0.9%; a 30d ATM needs ~3% to triple. The")
    print("      convexity long options require is simply not in the tape it")
    print("      selects. CAVEAT: MFE on 257/861 rows, short holds only —")
    print("      a longer-hold directional trade is a different question.\n")

    print("=" * 62)
    print("REQUIREMENTS CHECKLIST for profitable directional buying")
    print("=" * 62)
    reqs = [
        ("Directional accuracy ABOVE break-even for your R:R",
         f"FAIL — 33.6% measured, need 25-40% only IF winners are 3-5R, but",
         "       33.6% with the system's small winners nets negative"),
        ("Convex exit discipline: cut losers ~1R, ride winners 3-5R",
         "MISSING — the journal shows small wins / full-premium losses,",
         "       the opposite of the convexity long options require"),
        ("Enter at LOW IV (cheap premium) — high IV = bigger move needed",
         "OK now — VIX 12 is low; this part the system can time",
         ""),
        ("A real directional signal (edge over the implied move)",
         "FAIL — 15 hunts, no validated directional alpha in this universe",
         ""),
        ("Fast fills / tight spreads (theta clock is running)",
         "WEAK — manual execution, ~3-min stale stock data",
         ""),
        ("Position sizing for -EV-tail (most expire worthless)",
         "N/A until the edge exists — sizing a -EV bet just loses slower",
         ""),
    ]
    for i, (r, a, b) in enumerate(reqs, 1):
        print(f"\n{i}. {r}")
        print(f"   {a}")
        if b:
            print(f"   {b}")

    print("\n" + "=" * 62)
    print("VERDICT")
    print("=" * 62)
    print("Directional option buying is +EV ONLY with a real directional edge")
    print("(be right more than break-even) AND convex exits (winners >> losers).")
    print("This system has neither: 33.6% accuracy is below coin-flip, and its")
    print("payoff shape is inverted. The 'perfect way' to buy options is to")
    print("FIRST possess a directional edge — which the programme has shown is")
    print("absent here. Without it, buying is a slower version of the same PF 0.44.")
    print("\nThe requirement, in one line: an edge that predicts direction better")
    print("than the option's implied move. Build/find THAT first; the buying")
    print("mechanics are trivial once it exists, and pointless until it does.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
