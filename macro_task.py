"""macro_task.py — daily cycle for the paper macro fund (H-022).

    python macro_task.py --reset --capital 100000   # archive and start fresh
    python macro_task.py                            # daily: mark, rebalance if due
    python macro_task.py --status                   # read-only summary

Run after the US close. The book holds USD ETFs, so a mark taken while New
York is open is a mark against a forming bar — the same corruption the capture
layer already fights on the Indian side.

Order within a cycle is deliberate: MARK first so the rebalance sizes off a
NAV that reflects today's prices, then rebalance if the month has turned.

PAPER ONLY. No broker binding exists in this file.
"""
from __future__ import annotations

import argparse
import sys

from core import macro_fund as mf
from core.multi_asset import FUND_UNIVERSE, fetch, signals


def _prices(px) -> dict:
    """Last close per ticker."""
    return {c: float(px[c].iloc[-1]) for c in px.columns
            if px[c].notna().any()}


def cycle(verbose: bool = True) -> dict:
    state = mf.load_state()
    if not state:
        if verbose:
            print("no book yet — creating one at "
                  f"${mf.DEFAULT_CAPITAL:,.0f}")
        state = mf.reset()

    px = fetch(period="3y", universe=FUND_UNIVERSE)
    prices = _prices(px)
    if verbose:
        print(f"priced {len(prices)} of {len(FUND_UNIVERSE)} markets "
              f"as of {px.index[-1].date()}")

    state = mf.mark(state, prices)

    if mf.is_rebalance_day(state):
        targets = signals(px, universe=FUND_UNIVERSE)
        active = [t for t in targets if t["weight"]]
        if verbose:
            print(f"REBALANCE due — {len(active)} target positions")
        state = mf.rebalance(state, targets, prices)
    elif verbose:
        print(f"no rebalance due (last {state.get('last_rebalance')})")

    state = mf.record_curve(state)
    mf.save_state(state)
    return state


def show(state: dict) -> None:
    perf = mf.performance(state)
    exp = mf.exposure(state)
    ccy = state.get("base_currency", "USD")
    print("=" * 74)
    print(f"MACRO PAPER FUND — {state.get('mode', 'PAPER')} — {state.get('hypothesis')}")
    print("=" * 74)
    print(f"NAV        {ccy} {perf['nav']:>12,.2f}     "
          f"return {perf['total_return_pct']:+.2f}%")
    print(f"capital    {ccy} {perf['capital']:>12,.2f}     "
          f"realised {perf['realised_pnl']:+,.2f}  "
          f"unrealised {perf['unrealised_pnl']:+,.2f}")
    print(f"costs paid {ccy} {perf['total_costs']:>12,.2f}     "
          f"maxDD {perf['max_drawdown_pct']:.2f}%   "
          f"days live {perf['days_live']}")
    print(f"exposure   gross {exp['gross']:.2f}x   net {exp['net']:+.2f}x   "
          f"{exp['n_long']}L / {exp['n_short']}S")
    if not perf["mature"]:
        print(f"\n*** NOT MATURE — H-022 evaluates no earlier than "
              f"{perf['evaluate_after']}. Anything above is a progress")
        print("*** indicator, not a verdict. Do not act on it.")

    pos = sorted(state.get("positions", []),
                 key=lambda p: -abs(float(p.get("market_value") or 0)))
    if pos:
        print(f"\n{'MARKET':<16}{'CLASS':<11}{'DIR':<6}{'WEIGHT':>8}"
              f"{'ENTRY':>10}{'LAST':>10}{'P&L':>11}")
        print("-" * 72)
        for p in pos:
            print(f"{p['name']:<16}{p['asset_class']:<11}{p['direction']:<6}"
                  f"{p['weight']:>8.3f}{p['entry_price']:>10.2f}"
                  f"{float(p.get('last_price') or 0):>10.2f}"
                  f"{float(p.get('pnl') or 0):>+11.2f}")
    print("\nnet exposure by asset class:")
    for k, v in exp["by_class"].items():
        print(f"  {k:<11}{v:+.3f}x")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="archive and restart")
    ap.add_argument("--capital", type=float, default=mf.DEFAULT_CAPITAL)
    ap.add_argument("--status", action="store_true", help="read-only")
    args = ap.parse_args()

    if args.reset:
        mf.reset(args.capital)
        print(f"fresh book at ${args.capital:,.0f}")

    if args.status:
        state = mf.load_state()
        if not state:
            print("no book yet — run `python macro_task.py` to create one")
            return 1
        show(state)
        return 0

    state = cycle()
    print()
    show(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
