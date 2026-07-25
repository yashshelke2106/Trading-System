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
    equity_curve_points, horizon_accuracy, load_nifty, perf_summary,
    refresh_nifty_cache, top100_fno_composite,
)

STATE_FILE = os.path.join("logs", "allocation_state.json")
HISTORY_FILE = os.path.join("logs", "allocation_history.jsonl")
ALERT_FILE = os.path.join("logs", "allocation_alert.txt")


def build_status(refresh: bool = True) -> dict:
    # Freshness is recorded, never discarded: a failed refresh silently served
    # 3-day-old bars as a live target on 2026-07-24. Consumers read data_quality.
    rc = refresh_nifty_cache() if refresh else None

    nifty = load_nifty()

    out = {
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "instrument": ALLOCATION_CONFIG["core_instrument"],
        "data_quality": {
            "checked": rc is not None,
            "feed_ok": None if rc is None else rc.ok,
            "last_bar": None if rc is None else rc.last_date,
            "age_days": None if rc is None else rc.age_days(),
            "bars_added": None if rc is None else rc.added,
            "stale": None if rc is None else rc.is_stale(),
            "error": None if rc is None else (rc.error or None),
        },
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

    # comparison: top-100 F&O equal-weight composite (survivors-only — labeled)
    try:
        basket = top100_fno_composite()
        ix = basket.index.intersection(nifty.index)
        b, n = basket[ix], nifty[ix]
        out["comparison"] = {
            "label": "Top-100 F&O equal-weight",
            "caveat": ("survivors-only universe — measured ~+9.7pp/yr "
                       "survivorship inflation; monitoring only, not tradeable"),
            "backtest": perf_summary(b.pct_change().dropna()),
            "curve": {
                "basket": equity_curve_points(b),
                "nifty": equity_curve_points(n),
            },
        }
    except Exception as e:
        out["comparison"] = {"error": str(e)}

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
    dq = status.get("data_quality") or {}
    stale = bool(dq.get("stale"))

    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"asof": status["asof"], "state": cur["state"],
                            "nifty": status["nifty"], "ma200": status["ma200"],
                            "equity_weight": cur["equity_weight"],
                            "stale": stale}) + "\n")

    stale_msg = ""
    if stale:
        stale_msg = (
            f"STALE DATA: NIFTY cache last bar {dq.get('last_bar')} "
            f"({dq.get('age_days')}d old), feed_ok={dq.get('feed_ok')}"
            # ASCII only: this prints to the Windows console (cp1252), where a
            # non-ASCII dash renders as a replacement char in the scheduler log.
            + (f" - {dq['error']}" if dq.get("error") else "")
            + ". The target below is computed from OLD prices and is NOT current. "
              "Do NOT rebalance on it; fix the feed and re-run."
        )

    flipped = prev_state is not None and prev_state != cur["state"]
    near = abs(status["distance_to_flip_pct"]) <= 1.0
    if stale:
        # A stale run must never clear a real alert or look like a clean pass.
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            f.write(stale_msg + "\n")
        print("!" * 72)
        print("ALERT:", stale_msg)
        print("!" * 72)
    elif flipped or near:
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

    print(f"[{status['asof']}]{' [STALE]' if stale else ''} {cur['state']}  "
          f"{cur['equity_weight']*100:.0f}% equity / {cur['cash_weight']*100:.0f}% cash   "
          f"NIFTY {status['nifty']} vs 200DMA {status['ma200']} "
          f"({status['distance_to_flip_pct']:+.2f}%)")
    # Non-zero so the scheduler/log surfaces a dead feed instead of it passing
    # as a normal run — the whole point of this fix.
    return 2 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
