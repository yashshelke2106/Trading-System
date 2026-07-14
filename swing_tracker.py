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

from core.strategy_health import compute_health, health_line, load_health, save_health
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
    """Gap-honest walk. Returns resolution dict or None if still open.

    Two exit styles (row['exit_style']):
      'fixed' / absent — legacy: fixed target + stop + 10-session time exit.
      'C'              — adopted 2026-07-15 (docs/research/exit_style_gate.md):
                          stop 2xATR; WINNERS ride until the close crosses the
                          5-DMA against the position, then exit next open;
                          20-session cap. Cuts losses fast, lets winners run."""
    sig_d = pd.Timestamp(row["signal_date"])
    fut = bars[bars.index > sig_d]
    if len(fut) < 1:
        return None
    entry = float(fut.open.iloc[0])
    lng = row["direction"] == "long"
    sign = 1 if lng else -1
    tgt, stp = float(row["target"]), float(row["stop"])
    style_c = row.get("exit_style") == "C"
    max_hold = 20 if style_c else MAX_HOLD
    ma5 = bars.close.rolling(5).mean().reindex(fut.index)
    pending_exit = False
    for j in range(len(fut)):
        o, h, l, c = (float(fut.open.iloc[j]), float(fut.high.iloc[j]),
                      float(fut.low.iloc[j]), float(fut.close.iloc[j]))
        if pending_exit:                      # style C: momentum broke yesterday
            return _mk(row, entry, o, fut.index[j], "MOM_EXIT", sign)
        if j > 0:  # gap check at the open
            if (lng and o <= stp) or (not lng and o >= stp):
                return _mk(row, entry, o, fut.index[j], "SL_GAP", sign)
            if not style_c and ((lng and o >= tgt) or (not lng and o <= tgt)):
                return _mk(row, entry, o, fut.index[j], "TGT_GAP", sign)
        # intraday: stop BEFORE target (conservative)
        if (lng and l <= stp) or (not lng and h >= stp):
            return _mk(row, entry, stp, fut.index[j], "SL_HIT", sign)
        if not style_c and ((lng and h >= tgt) or (not lng and l <= tgt)):
            return _mk(row, entry, tgt, fut.index[j], "TARGET_HIT", sign)
        if style_c:
            m5 = float(ma5.iloc[j]) if not pd.isna(ma5.iloc[j]) else None
            in_profit = (c > entry) if lng else (c < entry)
            broke = m5 is not None and ((lng and c < m5) or (not lng and c > m5))
            if in_profit and broke:
                pending_exit = True           # exit at NEXT open (no lookahead)
        if j + 1 >= max_hold:
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
        print(health_line(load_health()))
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

    # decay monitor + persistence (stages 5 & 7) — pre-registered rules
    prev = load_health()
    health = compute_health(rows, prev_status=prev["status"] if prev else None)
    save_health(health)
    print(health_line(health))
    if health["status"] == "RETIRED":
        print("*** DECAY TRIGGER FIRED: fund NOTHING; paper bench continues. ***")

    if args.report:
        print()
        print(SwingLearner().report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
