"""
core/strategy_health.py — decay monitor + persistence score for the swing
sleeve (strategy lifecycle stages 5 & 7).

PRE-REGISTERED RULES (written 2026-07-07, BEFORE any live decay — changing
these after the fact defeats their purpose; treat edits like touching
PAPER_TRADE):

  Window      : rolling last 60 resolved FUNDED-side (long) paper trades.
  COLLECTING  : fewer than 30 resolved -> no verdict, keep collecting.
  HEALTHY     : rolling PF >= 1.00
  WARN        : rolling PF < 1.00, or persistence < 0.50 (edge mostly gone)
  RETIRED     : rolling PF < 0.90 with a FULL 60-trade window
                -> sleeve goes to CASH. Paper trading continues (the bench),
                   but nothing gets funded.
  REINSTATE   : while RETIRED, rolling PF >= 1.05 on the full window
                -> strategy returns to HEALTHY (hysteresis prevents flapping).

Persistence (the video's one good idea, computed honestly):
  persistence = live paper expectancy / backtest expectancy (same rules).
  Backtest baselines from the 15y run (logs/backtest_15y_trades.csv):
  long +18.1bp/trade net. 1.0 = full edge showing up live; 0.3 = mostly
  curve fit; negative = live is losing where the backtest won.

State: logs/strategy_health.json (written by swing_tracker after each
resolution batch; read by swing_screen and the swing app).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, List, Optional

STATE_FILE = os.path.join("logs", "strategy_health.json")

WINDOW = 60
MIN_EVAL = 30
RETIRE_PF = 0.90
REINSTATE_PF = 1.05
WARN_PF = 1.00
WARN_PERSISTENCE = 0.50
# 15y net-of-cost baselines. Long updated 2026-07-15 on adopting exit style C
# (ride winners until close<5DMA): +34.2bp replaces the fixed-target +18.1bp
# (docs/research/exit_style_gate.md). Short: no C-style 15y baseline yet ->
# None disables the persistence ratio for the paper bench.
BACKTEST_BASELINE_BP = {"long": 34.2, "short": None}


def _pf(rets: List[float]) -> float:
    gw = sum(r for r in rets if r > 0)
    gl = -sum(r for r in rets if r <= 0)
    return round(gw / gl, 3) if gl > 0 else float("inf")


def _side_stats(rows: List[dict], direction: str) -> Dict:
    rets = [float(r["ret_net"]) for r in rows
            if r.get("status") == "resolved" and r.get("direction") == direction]
    recent = rets[-WINDOW:]
    avg_bp = (sum(recent) / len(recent) * 1e4) if recent else 0.0
    base = BACKTEST_BASELINE_BP.get(direction, 0.0)
    persistence = round(avg_bp / base, 2) if base and recent else None
    return {
        "n_resolved": len(rets),
        "window_n": len(recent),
        "rolling_pf": _pf(recent) if recent else None,
        "rolling_avg_bp": round(avg_bp, 1),
        "backtest_baseline_bp": base,
        "persistence": persistence,
        "win_rate": round(sum(1 for r in recent if r > 0) / len(recent), 3) if recent else None,
    }


def compute_health(journal_rows: List[dict],
                   prev_status: Optional[str] = None) -> Dict:
    """Verdict comes from the FUNDED side (long). Shorts are reported as the
    paper bench only."""
    long_s = _side_stats(journal_rows, "long")
    short_s = _side_stats(journal_rows, "short")

    n, pf = long_s["window_n"], long_s["rolling_pf"]
    reasons = []
    if long_s["n_resolved"] < MIN_EVAL:
        status = "COLLECTING"
        reasons.append(f"only {long_s['n_resolved']}/{MIN_EVAL} resolved funded-side trades")
    elif prev_status == "RETIRED":
        if n >= WINDOW and pf is not None and pf >= REINSTATE_PF:
            status = "HEALTHY"
            reasons.append(f"REINSTATED: rolling PF {pf} >= {REINSTATE_PF} on full window")
        else:
            status = "RETIRED"
            reasons.append(f"still retired: rolling PF {pf} < reinstate bar {REINSTATE_PF}")
    elif n >= WINDOW and pf is not None and pf < RETIRE_PF:
        status = "RETIRED"
        reasons.append(f"DECAY TRIGGER: rolling-{WINDOW} PF {pf} < {RETIRE_PF} -> sleeve to CASH")
    elif (pf is not None and pf < WARN_PF) or (
            long_s["persistence"] is not None and long_s["persistence"] < WARN_PERSISTENCE):
        status = "WARN"
        if pf is not None and pf < WARN_PF:
            reasons.append(f"rolling PF {pf} < {WARN_PF}")
        if long_s["persistence"] is not None and long_s["persistence"] < WARN_PERSISTENCE:
            reasons.append(f"persistence {long_s['persistence']} < {WARN_PERSISTENCE} "
                           f"(live edge mostly below backtest)")
    else:
        status = "HEALTHY"
        reasons.append(f"rolling PF {pf} >= {WARN_PF}")

    return {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "reasons": reasons,
        "funded_side": long_s,
        "paper_bench_short": short_s,
        "rules": {
            "window": WINDOW, "min_eval": MIN_EVAL, "retire_pf": RETIRE_PF,
            "reinstate_pf": REINSTATE_PF, "warn_pf": WARN_PF,
            "warn_persistence": WARN_PERSISTENCE,
            "registered": "2026-07-07 (pre-registered; do not edit after decay)",
        },
    }


def save_health(health: Dict, path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(health, f, indent=2)
    os.replace(tmp, path)


def load_health(path: str = STATE_FILE) -> Optional[Dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def health_line(h: Optional[Dict]) -> str:
    if not h:
        return "Strategy health: no data yet (runs after first resolutions)"
    f = h["funded_side"]
    per = f"persistence={f['persistence']}" if f.get("persistence") is not None else "persistence=n/a"
    return (f"Strategy health: {h['status']} | rolling-{h['rules']['window']} "
            f"PF={f['rolling_pf']} avg={f['rolling_avg_bp']:+.1f}bp {per} "
            f"(n={f['n_resolved']} resolved) | {h['reasons'][0]}")
