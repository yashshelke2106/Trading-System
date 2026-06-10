"""
validate_meanrev_liquid.py — does restricting RSI-2 mean-reversion to the most
LIQUID names (where round-trip cost is genuinely low) lift it above costs?

Discipline:
  * "Liquid" = ranked by AVERAGE DAILY TURNOVER (close*volume) over the window.
    An a-priori, cost-justified criterion — NOT "the names that made money".
  * SAME fixed Connors RSI-2 rule from validate_meanrev.py (no tuning).
  * Honest tension: mean-reversion is usually STRONGER in small/volatile names
    (more overreaction) which have HIGHER costs. So the liquid tier may have a
    SMALLER gross edge AND a smaller cost. Net can go either way — that's the test.
  * Per-tier realistic round-trip cost is stated, and a full sweep is shown so
    nothing is cherry-picked. Held-out H2 at the tier's realistic cost.

Tier cost assumptions (large-cap stock-futures, buying weakness so slightly
worse fills):  Top-10/20 ~0.06% RT, Top-40 ~0.10%, All ~0.15%.

Run:  python validate_meanrev_liquid.py --full --limit 80 --days 1095
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from typing import Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_live_pipeline import fetch_daily, DEFAULT_UNIVERSE
from validate_meanrev import simulate_symbol, _stats, REGIME_SMA

SWEEP = [0.0005, 0.0008, 0.0012, 0.0020]
TIERS = [("Top 10", 10, 0.0006), ("Top 20", 20, 0.0006),
         ("Top 40", 40, 0.0010), ("All", 10_000, 0.0015)]


def _pf_str(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    if args.full:
        try:
            from core.universe import FO_UNIVERSE
            syms = list(dict.fromkeys(FO_UNIVERSE))
        except Exception:
            syms = DEFAULT_UNIVERSE
    else:
        syms = DEFAULT_UNIVERSE
    syms = syms[:args.limit] if args.limit else syms

    print(f"[MRL] fetching {len(syms)} names ({args.days}d) + ranking by turnover ...")
    recs: Dict[str, Dict] = {}
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is None or len(df) < REGIME_SMA + 15 or "volume" not in df.columns:
            continue
        turnover = float((df["close"] * df["volume"]).mean())
        recs[s] = {"turnover": turnover, "trades": simulate_symbol(df)}
    if not recs:
        print("[MRL] FATAL: no data (Dhan unreachable?).")
        return 1

    ranked = sorted(recs.items(), key=lambda kv: kv[1]["turnover"], reverse=True)
    print(f"[MRL] {len(ranked)} names with data. Most liquid (by avg daily turnover ₹):")
    for s, r in ranked[:10]:
        print(f"   {s:14s} ₹{r['turnover']/1e7:8.1f} cr/day  trades={len(r['trades'])}")

    print("\n" + "=" * 78)
    print("  RSI-2 MEAN REVERSION BY LIQUIDITY TIER  (gross + cost sweep + held-out H2)")
    print("=" * 78)
    print(f"  {'tier':8} {'names':>5} {'trades':>6} {'grossPF':>7} "
          f"{'net@.05':>7} {'net@.08':>7} {'net@.12':>7} {'H2@tier':>8}  verdict")

    any_pass = False
    for name, k, tier_cost in TIERS:
        tier_syms = [s for s, _ in ranked[:k]]
        trades = [t for s in tier_syms for t in recs[s]["trades"]]
        if len(trades) < 30:
            continue
        trades.sort(key=lambda t: t["date"])
        raw = np.array([t["raw"] for t in trades])
        gross = _stats(raw)
        n5 = _stats(raw - 0.0005)
        n8 = _stats(raw - 0.0008)
        n12 = _stats(raw - 0.0012)
        # held-out H2 at this tier's realistic cost
        half = len(raw) // 2
        h2 = _stats(raw[half:] - tier_cost)
        full_tier = _stats(raw - tier_cost)
        passes = (full_tier["pf"] > 1.2 and full_tier["exp"] > 0
                  and h2["pf"] > 1.1 and h2["exp"] > 0)
        any_pass = any_pass or passes
        verdict = (f"PASS @{tier_cost*100:.2f}%" if passes
                   else f"fail @{tier_cost*100:.2f}%")
        print(f"  {name:8} {len(tier_syms):>5} {len(raw):>6} "
              f"{_pf_str(gross['pf']):>7} {_pf_str(n5['pf']):>7} "
              f"{_pf_str(n8['pf']):>7} {_pf_str(n12['pf']):>7} "
              f"{_pf_str(h2['pf']):>8}  {verdict}")

    print("=" * 78)
    print("\n  VERDICT")
    print("  " + "-" * 40)
    if any_pass:
        print("  A liquid tier clears net PF>1.2 with the held-out half holding, at a")
        print("  defensible cost. This is a REAL (small) lead — but still survivor-biased")
        print("  and single-regime. Next: point-in-time universe + a drawdown period +")
        print("  real per-symbol F&O costs. Do NOT scale size on this alone.")
    else:
        print("  No liquidity tier clears net PF>1.2 out-of-sample at a defensible cost.")
        print("  The gross effect is real but the liquid names don't carry enough of it to")
        print("  beat costs, and the cheap-cost tier doesn't have a big enough edge.")
        print("  HONEST CONCLUSION: no economically-tradeable edge in this approach.")
        print("  The right call is to STOP building and preserve capital, not to tune.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
