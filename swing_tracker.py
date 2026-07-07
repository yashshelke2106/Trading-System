"""
swing_tracker.py — resolve open paper swing trades and TEACH the learner.

For each OPEN entry in logs/swing_paper_journal.jsonl:
  entry  = first session open AFTER the signal date (no same-close mirage)
  exits  = gap-honest: open beyond stop/target fills at the open;
           intraday, stop is checked BEFORE target (conservative);
           else time exit at close of session 10.
  costs  = long 0.25% RT (cash delivery) / short 0.10% RT (stock futures).
Every resolution — win or loss, long or short — updates the SwingLearner
bucket identically. Run daily (scheduler) or ad hoc:

    python swing_tracker.py
    python swing_tracker.py --report     # learner report after resolving
"""

from __future__ import annotations

import argparse
import json
import os
import warnings

import pandas as pd

warnings.filterwarnings("ignore")

from core.swing_learner import SwingLearner
from swing_screen import JOURNAL_FILE, YMAP

MAX_HOLD = 10
COST = {"long": 0.0025, "short": 0.0010}


def _load_journal() -> list:
    if not os.path.exists(JOURNAL_FILE):
        return []
    rows = []
    for line in open(JOURNAL_FILE, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _resolve(row: dict, bars: pd.DataFrame) -> dict | None:
    """Gap-honest walk. Returns resolution dict or None if still open."""
    sig_d = pd.Timestamp(row["signal_date"])
    fut = bars[bars.index > sig_d]
    if len(fut) < 1:
        return None
    entry = float(fut.open.iloc[0])
    lng = row["direction"] == "long"
    sign = 1 if lng else -1
    tgt, stp = float(row["target"]), float(row["stop"])
    for j in range(len(fut)):
        o, h, l, c = (float(fut.open.iloc[j]), float(fut.high.iloc[j]),
                      float(fut.low.iloc[j]), float(fut.close.iloc[j]))
        if j > 0:  # gap check at the open
            if (lng and o <= stp) or (not lng and o >= stp):
                return _mk(row, entry, o, fut.index[j], "SL_GAP", sign)
            if (lng and o >= tgt) or (not lng and o <= tgt):
                return _mk(row, entry, o, fut.index[j], "TGT_GAP", sign)
        # intraday: stop BEFORE target (conservative)
        if (lng and l <= stp) or (not lng and h >= stp):
            return _mk(row, entry, stp, fut.index[j], "SL_HIT", sign)
        if (lng and h >= tgt) or (not lng and l <= tgt):
            return _mk(row, entry, tgt, fut.index[j], "TARGET_HIT", sign)
        if j + 1 >= MAX_HOLD:
            return _mk(row, entry, c, fut.index[j], "TIME_EXIT", sign)
    return None


def _mk(row, entry, exit_px, when, outcome, sign) -> dict:
    gross = sign * (exit_px / entry - 1)
    net = gross - COST[row["direction"]]
    return {"entry_px": round(entry, 2), "exit_px": round(float(exit_px), 2),
            "exit_date": str(pd.Timestamp(when).date()), "outcome": outcome,
            "ret_gross": round(gross, 5), "ret_net": round(net, 5),
            "won": net > 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    rows = _load_journal()
    open_rows = [r for r in rows if r.get("status") == "open"]
    if not open_rows:
        print("no open paper trades to resolve")
        if args.report:
            print(SwingLearner().report())
        return 0

    import yfinance as yf
    syms = sorted({r["symbol"] for r in open_rows})
    tickers = {s: YMAP.get(s, s) + ".NS" for s in syms}
    raw = yf.download(list(tickers.values()), period="6mo", group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)

    learner = SwingLearner()
    resolved = 0
    for r in open_rows:
        try:
            d = raw[tickers[r["symbol"]]][["Open", "High", "Low", "Close"]].dropna()
            d.columns = ["open", "high", "low", "close"]
        except Exception:
            continue
        res = _resolve(r, d)
        if res is None:
            continue
        r.update(res)
        r["status"] = "resolved"
        learner.record(r["direction"], r["signal"], r.get("regime", "risk_on"),
                       res["won"], res["ret_net"], symbol=r["symbol"])
        resolved += 1
        print(f"  {r['symbol']:<13} {r['direction']:<5} {res['outcome']:<10} "
              f"entry={res['entry_px']} exit={res['exit_px']} "
              f"net={res['ret_net']*100:+.2f}%")
    learner.save()

    tmp = JOURNAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, JOURNAL_FILE)

    still = sum(1 for r in rows if r.get("status") == "open")
    print(f"resolved {resolved}, still open {still}, learner updated")
    if args.report:
        print()
        print(SwingLearner().report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
