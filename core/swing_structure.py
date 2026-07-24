"""
swing_structure.py — swing-scale movement state and point-in-time event log.

WHAT THIS IS
------------
The "swing pattern" half of the movement-capture layer (day_structure.py is
the intraday half). From daily bars it records:

  STATE  (recomputed on demand)   confirmed swing pivots, the current leg
                                  (direction / age / extent), consolidation
                                  boxes, distance from 20d and 50d highs.
  EVENTS (appended, permanent)    logs/swing_events.jsonl — one row when a
                                  structural thing HAPPENS: a 20d-high
                                  breakout run starts, a box resolves, a
                                  pivot gets confirmed. Forward-return slots
                                  are left null and filled by --analyze once
                                  the horizon has elapsed (same forward-
                                  collector pattern as news_reaction.py).

Movement description only. No signal, no expected direction, no size. The
breakout events exist so "do 20d breakouts pay net of cost on THIS universe?"
can one day be answered from a point-in-time log instead of a survivors-only
re-scan — they are evidence for a future statistician gate, not entries.

THE LOOKAHEAD RULE (the one honesty constraint that matters here)
-----------------------------------------------------------------
A swing pivot at bar i is only knowable at bar i+W (it needs W lower highs /
higher lows after it). Every pivot therefore carries BOTH dates:
  pivot_date    when the extreme printed
  confirm_date  when it became knowable (i+W)  <- the event timestamp
Anything that consumes pivot events must key on confirm_date. Keying on
pivot_date is lookahead and will inflate whatever is measured against it.
Breakout events have no such lag — close > prior 20d high is knowable at that
day's close — so their event date is the trigger day itself, and the level is
computed from the PRIOR 20 days (bar t excluded).

DATA SOURCE
-----------
Daily bars via market_state.daily_bars (bhavcopy archive preferred — point-in-
time, includes delisted names — yfinance fallback). Events backfilled from
archive history are honest: each day's test uses only bars <= that day.

RUN
---
    python -m core.swing_structure --state RELIANCE
    python -m core.swing_structure --update --universe top100
    python -m core.swing_structure --analyze          # fill elapsed fwd slots
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import warnings
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_PATH = os.path.join(_ROOT, "logs", "swing_events.jsonl")

PIVOT_W = 5                 # bars each side to confirm a swing point
BREAKOUT_LOOKBACK = 20      # close > prior N-day high starts a breakout run
BOX_DAYS = 15               # consolidation window
BOX_MAX_RANGE_PCT = 6.0     # box = last BOX_DAYS high/low span under this
FWD_HORIZONS = (1, 5, 10, 20)


# ── Pivots ──────────────────────────────────────────────────────────────────

def confirmed_pivots(df: pd.DataFrame, w: int = PIVOT_W) -> List[Dict]:
    """Swing pivots with their confirmation dates.

    A high at i is a pivot iff it is the max of [i-w, i+w]; it becomes
    knowable at index i+w. Bars closer than w to the end of the frame can
    not yet be confirmed and are NOT returned — that is the no-lookahead
    guarantee, not an off-by-one.
    """
    h, l = df["high"].values, df["low"].values
    idx = df.index
    out: List[Dict] = []
    for i in range(w, len(df) - w):
        win_h = h[i - w:i + w + 1]
        win_l = l[i - w:i + w + 1]
        if h[i] == win_h.max():
            out.append({"kind": "high", "pivot_date": idx[i], "price": float(h[i]),
                        "confirm_date": idx[i + w]})
        if l[i] == win_l.min():
            out.append({"kind": "low", "pivot_date": idx[i], "price": float(l[i]),
                        "confirm_date": idx[i + w]})
    out.sort(key=lambda p: p["pivot_date"])
    return out


# ── State ───────────────────────────────────────────────────────────────────

def swing_state(symbol: str, df: Optional[pd.DataFrame] = None) -> Dict:
    """Current swing-scale description of one symbol. Pure readout."""
    if df is None:
        from core.market_state import daily_bars
        df = daily_bars(symbol)
    if df is None or len(df) < BREAKOUT_LOOKBACK + PIVOT_W * 2:
        return {"symbol": symbol, "error": "insufficient daily history"}

    piv = confirmed_pivots(df)
    close = float(df["close"].iloc[-1])
    asof = df.index[-1]

    leg = None
    if piv:
        last = piv[-1]
        leg_dir = "up" if last["kind"] == "low" else "down"
        leg = {
            "direction": leg_dir,
            "from_price": last["price"],
            "from_date": str(last["pivot_date"].date()),
            "confirmed": str(last["confirm_date"].date()),
            "age_bars": int((df.index > last["pivot_date"]).sum()),
            "extent_pct": round((close - last["price"]) / last["price"] * 100, 2),
        }

    tail = df.tail(BOX_DAYS)
    box_hi, box_lo = float(tail["high"].max()), float(tail["low"].min())
    box_range_pct = round((box_hi - box_lo) / close * 100, 2) if close else None
    in_box = box_range_pct is not None and box_range_pct <= BOX_MAX_RANGE_PCT

    prior = df.iloc[:-1]
    hi20 = float(prior["high"].tail(BREAKOUT_LOOKBACK).max())
    hi50 = float(prior["high"].tail(50).max())

    return {
        "symbol": symbol,
        "asof": str(asof.date()),
        "close": round(close, 2),
        "leg": leg,
        "pivots_confirmed": len(piv),
        "box": {"in_box": in_box, "high": round(box_hi, 2),
                "low": round(box_lo, 2), "range_pct": box_range_pct,
                "days": BOX_DAYS},
        "dist_20d_high_pct": round((close - hi20) / hi20 * 100, 2),
        "dist_50d_high_pct": round((close - hi50) / hi50 * 100, 2),
    }


# ── Events ──────────────────────────────────────────────────────────────────

def detect_events(symbol: str, df: pd.DataFrame) -> List[Dict]:
    """Walk the daily history and emit point-in-time structural events.

    Honest by construction: the test at bar t reads only bars <= t.
    breakout_20d fires on the FIRST day of a run (previous close was not
    above ITS prior 20d high), so a week above the level is one event.
    """
    events: List[Dict] = []
    n = len(df)
    if n < BREAKOUT_LOOKBACK + 2:
        return events

    c = df["close"].values
    h = df["high"].values
    idx = df.index

    # Rolling max of the PRIOR 20 highs (bar t excluded via shift).
    prior_hi = pd.Series(h).shift(1).rolling(BREAKOUT_LOOKBACK).max().values

    above = c > prior_hi
    for t in range(BREAKOUT_LOOKBACK + 1, n):
        if above[t] and not above[t - 1]:
            events.append({
                "event": "breakout_20d",
                "symbol": symbol,
                "date": str(idx[t].date()),
                "close": round(float(c[t]), 2),
                "level": round(float(prior_hi[t]), 2),
            })

    for p in confirmed_pivots(df):
        events.append({
            "event": f"pivot_{p['kind']}",
            "symbol": symbol,
            "date": str(p["confirm_date"].date()),     # knowable date
            "pivot_date": str(p["pivot_date"].date()),
            "close": round(float(df.loc[p["confirm_date"], "close"]), 2),
            "level": round(p["price"], 2),
        })

    return events


def _event_key(e: Dict) -> Tuple:
    return (e["symbol"], e["event"], e["date"])


def _load_events() -> List[Dict]:
    if not os.path.exists(EVENTS_PATH):
        return []
    out = []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def update(symbols: List[str]) -> Dict:
    """Append newly detected events for `symbols`. Idempotent via event key."""
    from core.market_state import daily_bars

    existing: Set[Tuple] = {_event_key(e) for e in _load_events()}
    added, failed = 0, []

    os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        for sym in symbols:
            df = daily_bars(sym)
            if df is None or df.empty:
                failed.append(sym)
                continue
            for e in detect_events(sym, df):
                if _event_key(e) not in existing:
                    f.write(json.dumps(e) + "\n")
                    existing.add(_event_key(e))
                    added += 1

    return {"symbols": len(symbols), "added": added,
            "total": len(existing), "failed": failed[:20]}


def analyze() -> Dict:
    """Fill forward-return slots on events whose horizon has elapsed.

    Raw close-to-close returns from the event day, per horizon. Descriptive
    only — no cost model, no verdict. Cost-netting and significance are the
    statistician's job if these events are ever promoted to a hypothesis.
    """
    from core.market_state import daily_bars

    events = _load_events()
    if not events:
        return {"events": 0, "filled": 0}

    bars_cache: Dict[str, Optional[pd.DataFrame]] = {}
    filled = 0
    for e in events:
        want = [k for k in FWD_HORIZONS if f"fwd_{k}d_pct" not in e]
        if not want:
            continue
        sym = e["symbol"]
        if sym not in bars_cache:
            bars_cache[sym] = daily_bars(sym)
        df = bars_cache[sym]
        if df is None or df.empty:
            continue
        # daily_bars mixes sources: bhavcopy frames are tz-naive, the
        # yfinance fallback is tz-aware IST. Strip tz before comparing
        # against the naive event-date string or searchsorted raises.
        idx = df.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        dates = idx.normalize()
        pos = dates.searchsorted(pd.Timestamp(e["date"]))
        if pos >= len(df) or str(dates[pos].date()) != e["date"]:
            continue
        c0 = float(df["close"].iloc[pos])
        changed = False
        for k in want:
            if pos + k < len(df):
                ck = float(df["close"].iloc[pos + k])
                e[f"fwd_{k}d_pct"] = round((ck - c0) / c0 * 100, 3)
                changed = True
        filled += changed

    with open(EVENTS_PATH, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    return {"events": len(events), "filled": filled}


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Swing-scale movement state + events.")
    ap.add_argument("--state", metavar="SYMBOL")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--universe", default="top100", choices=["fo", "top100"])
    ap.add_argument("symbols", nargs="*")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    if args.state:
        print(json.dumps(swing_state(args.state.upper()), indent=2))
        return 0

    if args.update:
        symbols = [s.upper() for s in args.symbols]
        if not symbols:
            from core.universe import FO_UNIVERSE, TOP100_LIQUID
            symbols = list(FO_UNIVERSE if args.universe == "fo" else TOP100_LIQUID)
        res = update(symbols)
        print(f"Symbols   : {res['symbols']}")
        print(f"New events: {res['added']}")
        print(f"Total     : {res['total']}")
        if res["failed"]:
            print(f"Failed    : {', '.join(res['failed'])}")
        return 0

    if args.analyze:
        res = analyze()
        print(f"Events on file      : {res['events']}")
        print(f"Forward slots filled: {res['filled']}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
