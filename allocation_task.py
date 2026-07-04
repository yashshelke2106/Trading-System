"""
allocation_task.py — path-#1 autopilot tick. Designed for Windows Task Scheduler
(weekday mornings). Does one honest pass:

  1. refresh NIFTY daily bars (yfinance fallback; stale cache stands on failure)
  2. compute today's target allocation (200-DMA overlay variant)
  3. detect a state flip vs the last saved state -> logs/allocation_alert.txt
  4. persist logs/allocation_state.json (dashboard/API reads this)
  5. append logs/allocation_history.jsonl (audit trail)

No orders are placed. Execution stays manual (Dhan/DEXT T3), PAPER_TRADE stays True.

    python allocation_task.py
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime

from core.allocation import (
    ALLOCATION_CONFIG, backtest_allocation, compute_target_allocation,
    horizon_accuracy, load_nifty, perf_summary, refresh_nifty_cache,
)

STATE_FILE = os.path.join("logs", "allocation_state.json")
HISTORY_FILE = os.path.join("logs", "allocation_history.jsonl")
ALERT_FILE = os.path.join("logs", "allocation_alert.txt")


def build_status(refresh: bool = True) -> dict:
    if refresh:
        refresh_nifty_cache()
    nifty = load_nifty()

    out = {
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "instrument": ALLOCATION_CONFIG["core_instrument"],
        "asof": None,
        "targets": {},
        "backtest": {},
        "horizon_accuracy": {},
    }
    for name, overlay in (("core", False), ("overlay", True)):
        cfg = copy.deepcopy(ALLOCATION_CONFIG)
        cfg["trend_overlay"]["enabled"] = overlay
        t = compute_target_allocation(nifty, cfg=cfg)
        out["asof"] = t.asof
        out["targets"][name] = {
            "state": t.state, "equity_weight": t.equity_weight,
            "cash_weight": t.cash_weight, "note": t.note,
        }
        r = backtest_allocation(nifty, cfg=cfg)
        out["backtest"][name] = perf_summary(r)
        out["horizon_accuracy"][name] = horizon_accuracy(r)

    # distance to the flip line (overlay decision variable)
    ma = nifty.rolling(ALLOCATION_CONFIG["trend_overlay"]["ma_window"]).mean().iloc[-1]
    last = float(nifty.iloc[-1])
    out["nifty"] = round(last, 2)
    out["ma200"] = round(float(ma), 2)
    out["distance_to_flip_pct"] = round((last / float(ma) - 1) * 100, 2)
    return out


def main() -> int:
    prev_state = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                prev_state = json.load(f)["targets"]["overlay"]["state"]
        except Exception:
            prev_state = None

    status = build_status(refresh=True)
    cur = status["targets"]["overlay"]

    os.makedirs("logs", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"asof": status["asof"], "state": cur["state"],
                            "nifty": status["nifty"], "ma200": status["ma200"],
                            "equity_weight": cur["equity_weight"]}) + "\n")

    flipped = prev_state is not None and prev_state != cur["state"]
    near = abs(status["distance_to_flip_pct"]) <= 1.0
    if flipped or near:
        msg = (f"[{status['asof']}] "
               + (f"STATE FLIP: {prev_state} -> {cur['state']}. " if flipped else "")
               + (f"NEAR FLIP: NIFTY {status['nifty']} is "
                  f"{status['distance_to_flip_pct']:+.2f}% from 200DMA "
                  f"{status['ma200']}. " if near and not flipped else "")
               + f"Target: {cur['equity_weight']*100:.0f}% equity / "
                 f"{cur['cash_weight']*100:.0f}% cash ({status['instrument']}). "
                 f"Rebalance manually in Dhan.")
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
        print("ALERT:", msg)
    else:
        # clear stale alert once resolved
        if os.path.exists(ALERT_FILE):
            os.remove(ALERT_FILE)

    print(f"[{status['asof']}] {cur['state']}  "
          f"{cur['equity_weight']*100:.0f}% equity / {cur['cash_weight']*100:.0f}% cash   "
          f"NIFTY {status['nifty']} vs 200DMA {status['ma200']} "
          f"({status['distance_to_flip_pct']:+.2f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
