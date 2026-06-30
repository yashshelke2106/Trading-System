"""
allocation_monitor.py — path-#1 monitor. Prints the CURRENT target allocation
and the honest backtest profile of the index-core strategy (with/without the
trend overlay). Run anytime to see what to hold.

    python allocation_monitor.py            # core-only (default)
    python allocation_monitor.py --overlay  # with 200-DMA drawdown control
"""

from __future__ import annotations

import argparse
import copy
import os

import pandas as pd

from core.allocation import (
    ALLOCATION_CONFIG, compute_target_allocation, backtest_allocation, perf_summary,
)


def _load_nifty() -> pd.Series:
    p = os.path.join("logs", "bar_cache", "NIFTY.parquet")
    return pd.read_parquet(p).sort_index()["close"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlay", action="store_true", help="enable 200-DMA trend overlay")
    args = ap.parse_args()

    nifty = _load_nifty()
    cfg = copy.deepcopy(ALLOCATION_CONFIG)
    cfg["trend_overlay"]["enabled"] = args.overlay

    t = compute_target_allocation(nifty, cfg=cfg)
    print("=" * 60)
    print("PATH-#1 ALLOCATION — capture the equity premium cheaply")
    print("=" * 60)
    print(f"As of {t.asof}   [{t.state}]  {t.note}")
    print(f"  TARGET: {t.equity_weight*100:.0f}% equity ({cfg['core_instrument']})"
          f"   {t.cash_weight*100:.0f}% cash/liquid")
    print()

    # honest backtest: both variants for comparison
    print("Backtest (net of switch cost, ETF expense, cash yield):")
    for name, ov in [("index core (buy & hold)", False), ("core + 200-DMA overlay", True)]:
        c = copy.deepcopy(ALLOCATION_CONFIG); c["trend_overlay"]["enabled"] = ov
        p = perf_summary(backtest_allocation(nifty, cfg=c))
        print(f"  {name:26} CAGR={p['cagr']*100:6.2f}%  vol={p['vol']*100:5.1f}%  "
              f"Sharpe={p['sharpe']:5.2f}  maxDD={p['max_dd']*100:6.1f}%  ({p['years']}y)")
    print()
    print("Honest note: you earn ~the market return with low cost + discipline,")
    print("not alpha. Overlay trades return for shallower drawdown — your risk call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
