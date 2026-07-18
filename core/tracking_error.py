"""
Tracking-error monitor - does live paper performance match the backtest?

WHY:
    A strategy that drifts from its backtest is broken EITHER way: worse means
    decay/execution slippage; better means the backtest was wrong (and sizing
    decisions built on it are wrong too). The promotion diagram's rule applies:
    "every live strategy that drifts is killed without sentiment."

METHOD:
    Live cohort = closed journal trades for the strategy (engine_version
    prefix). Live profit factor = sum(wins)/|sum(losses)| on pnl_pct.
    Bootstrap the trades -> 95% CI for live PF. Compare against the
    backtest-expected PF. Expected PF outside the live CI -> DRIFT flag.

EXPECTED_PF source: audit 2026-06-08 (honest_metrics.py re-run of the
india_swing v3 backtest): PF 0.72. Override with --expected when the
backtest is re-run.

RUN:  python -m core.tracking_error
      python -m core.tracking_error --engine v3-swing --expected 0.72
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from typing import Dict, List, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(_ROOT, "logs", "signal_journal.jsonl")

EXPECTED_PF = 0.72          # audit 2026-06-08, india_swing v3 backtest
ENGINE_PREFIX = "v3-swing"  # current strategy cohort
_BOOT = 4000
MIN_TRADES = 30             # below this, CI is too wide to mean anything


def _live_trades(engine_prefix: str) -> List[float]:
    """pnl_pct of closed trades for the engine cohort."""
    if not os.path.exists(JOURNAL):
        return []
    out = []
    with open(JOURNAL, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ev = r.get("engine_version") or ""
            if not ev.startswith(engine_prefix):
                continue
            p = r.get("pnl_pct")
            if p is None or not r.get("outcome"):
                continue
            out.append(float(p))
    return out


def _pf(trades: List[float]) -> float:
    wins = sum(t for t in trades if t > 0)
    losses = -sum(t for t in trades if t < 0)
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def _boot_ci(trades: List[float]) -> Tuple[float, float]:
    pfs = []
    n = len(trades)
    for _ in range(_BOOT):
        sample = [trades[random.randrange(n)] for _ in range(n)]
        pfs.append(_pf(sample))
    pfs.sort()
    return pfs[int(0.025 * len(pfs))], pfs[int(0.975 * len(pfs))]


def run(engine_prefix: str = ENGINE_PREFIX,
        expected_pf: float = EXPECTED_PF) -> Dict:
    trades = _live_trades(engine_prefix)
    print("=" * 66)
    print(f"TRACKING ERROR  cohort=engine '{engine_prefix}*'  "
          f"expected PF {expected_pf:.2f} (backtest)")
    print("=" * 66)

    if len(trades) < MIN_TRADES:
        print(f"  only {len(trades)} closed trades (<{MIN_TRADES}) - "
              f"too few to compare. Keep paper trading.")
        return {"status": "INSUFFICIENT", "n": len(trades)}

    live_pf = _pf(trades)
    lo, hi = _boot_ci(trades)
    wr = sum(1 for t in trades if t > 0) / len(trades)
    avg = sum(trades) / len(trades)

    drift = not (lo <= expected_pf <= hi)
    direction = ("BETTER than backtest" if live_pf > expected_pf
                 else "WORSE than backtest")
    status = f"DRIFT ({direction})" if drift else "WITHIN expectation"

    print(f"  trades: {len(trades)}   win rate: {wr:.1%}   "
          f"avg pnl: {avg:+.2f}%")
    print(f"  live PF: {live_pf:.2f}   95% CI [{lo:.2f}, {hi:.2f}]")
    print(f"  verdict: {status}")
    if drift:
        print("  ACTION: drift is a kill/repair signal EITHER way -")
        print("    worse -> strategy decayed or costs mis-modeled: de-risk now;")
        print("    better -> backtest is wrong: fix backtest BEFORE trusting sizing.")
    print()
    return {"status": "DRIFT" if drift else "OK", "n": len(trades),
            "live_pf": round(live_pf, 3), "ci": [round(lo, 3), round(hi, 3)],
            "expected_pf": expected_pf, "win_rate": round(wr, 3)}


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Live-vs-backtest tracking error")
    p.add_argument("--engine", type=str, default=ENGINE_PREFIX)
    p.add_argument("--expected", type=float, default=EXPECTED_PF)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args(argv)
    random.seed(a.seed)
    run(a.engine, a.expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
