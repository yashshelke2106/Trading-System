"""
harvest.py — the daily driver for a premium-HARVEST book (not an edge hunt).

THE POSTURE CHANGE THIS ENCODES
-------------------------------
The programme spent 15 registered hypotheses proving there is no tradeable
ALPHA at retail scale in this universe. The decision (docs/research/
path_to_profitable.md) is to stop hunting edge and harvest the two premia that
are real:

  EQUITY RISK PREMIUM   — validated. Captured by the index core + 200-DMA
                          overlay (core.allocation / allocation_task).
  VOLATILITY RISK PREMIUM — thin, unproven OOS (t=0.42), tail-capped. Held as a
                          small defined-risk condor sleeve that the paper book
                          is forward-testing, NOT sized as the engine.

This driver does the daily harvest chores and NOTHING that hunts edge. In
particular it does NOT run scan_only_v2 — that scanner finds the PF-0.44
net-negative signals, and funding them is the opposite of the goal. If you want
to watch it, run it yourself; it is deliberately not in the harvest loop.

WHAT IT DOES EACH DAY
---------------------
  1. reads the allocation overlay -> today's equity target (risk_on/off)
  2. reads the swing_book plan    -> condor sizing + ladder for the sleeve
  3. marks the paper book to market and prints P&L WITH its confidence caveat
  4. flags ACTIONS only when something actually changed:
       - overlay flipped (risk_on <-> risk_off) -> rebalance equity
       - a condor ladder tranche is due
     No action on a quiet day is the correct, common outcome. Harvesting is
     mostly waiting.

RUN
---
    python harvest.py                 # daily harvest report
    python harvest.py --capital 100000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_STATE = os.path.join("logs", "harvest_state.json")


def _prev_overlay() -> str:
    try:
        with open(_STATE, encoding="utf-8") as fh:
            return json.load(fh).get("overlay_state", "")
    except Exception:
        return ""


def _save_overlay(state: str) -> None:
    os.makedirs("logs", exist_ok=True)
    try:
        with open(_STATE, "w", encoding="utf-8") as fh:
            json.dump({"overlay_state": state,
                       "asof": datetime.now(timezone.utc).isoformat()}, fh)
    except Exception:
        pass


def harvest(capital: float = 100000.0) -> dict:
    out = {"asof": datetime.now(timezone.utc).date().isoformat(),
           "capital": capital, "actions": []}

    # 1. Book plan (equity target + condor sizing), all from live data.
    try:
        from core.swing_book import plan as book_plan
        p = book_plan(capital)
        out["plan"] = p.to_dict()
        eq = p.equity
        op = p.options
    except Exception as e:
        out["plan_error"] = str(e)
        eq = op = {}

    # 2. Overlay change detection -> the only equity action that matters.
    overlay = eq.get("overlay_state", "unknown")
    prev = _prev_overlay()
    if prev and overlay != prev and overlay != "unknown":
        out["actions"].append(
            f"OVERLAY FLIP {prev} -> {overlay}: rebalance equity sleeve to "
            f"{eq.get('overlay_equity_weight')} weight")
    _save_overlay(overlay)

    # 3. Mark the paper book.
    try:
        from core.paper_book import mark, status
        mark()
        out["book"] = status()
    except Exception as e:
        out["book_error"] = str(e)

    # 3b. Pre-registered proof vs benchmark — the "is it working" verdict.
    try:
        from core.proof import evaluate as proof_eval
        out["proof"] = proof_eval().to_dict()
    except Exception as e:
        out["proof_error"] = str(e)

    # 4. Condor tranche reminder (time-laddered, not stacked).
    if op.get("fundable") and op.get("ladder_tranches"):
        out["condor_ladder"] = op["ladder_tranches"]
        out["condor_note"] = ("enter tranches ~10 days apart; time is this "
                              "sleeve's only diversification")

    return out


def _print(o: dict) -> None:
    print(f"\n=== DAILY HARVEST — Rs{o['capital']:,.0f}  ({o['asof']}) ===")
    print("posture: HARVEST premium (equity beta + defined-risk VRP). "
          "NOT hunting edge.\n")

    eq = (o.get("plan") or {}).get("equity", {})
    op = (o.get("plan") or {}).get("options", {})
    if eq:
        print(f"EQUITY  overlay={eq.get('overlay_state')} "
              f"(wt {eq.get('overlay_equity_weight')})", end="")
        if eq.get("nifty"):
            print(f"  NIFTY {eq['nifty']:,.0f} vs 200DMA {eq.get('ma200'):,.0f}")
        else:
            print()
        if eq.get("fundable"):
            print(f"        hold {eq.get('units')} NIFTYBEES "
                  f"(Rs{eq.get('deployed', 0):,.0f}), cash Rs{eq.get('cash_in_sleeve', 0):,.0f}")
    if op.get("fundable"):
        print(f"CONDOR  {op.get('lots')} lot(s) {op['chosen']['wing_points']}pt wings, "
              f"risk Rs{op.get('capital_at_risk', 0):,.0f}, ladder {op.get('ladder_tranches')}")

    b = o.get("book") or {}
    if b.get("ok"):
        print(f"\nPAPER BOOK  total P&L Rs{b['total_pnl']:,.2f} ({b['return_pct']:+.3f}%)"
              f"  day {b['days_running']}")
        print(f"  {b['confidence']}")

    pr = o.get("proof") or {}
    if pr.get("verdict"):
        print(f"\nPROOF [{pr.get('checkpoint')}]  {pr['verdict']}")
        if pr.get("benchmark_return_pct") is not None:
            print(f"  book {pr['book_return_pct']:+.2f}% vs NIFTY "
                  f"{pr['benchmark_return_pct']:+.2f}% (excess "
                  f"{pr['excess_pct']:+.2f}%, capture {pr['capture_ratio']})")

    print("\nACTIONS:")
    if o["actions"]:
        for a in o["actions"]:
            print(f"  ! {a}")
    else:
        print("  none — hold. (harvesting is mostly waiting; a quiet day is normal)")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily premium-harvest driver.")
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    o = harvest(args.capital)
    if args.json:
        print(json.dumps(o, indent=2, default=str))
    else:
        _print(o)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
