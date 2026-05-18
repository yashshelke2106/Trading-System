"""Accuracy report — per-engine signal performance.

Splits the journal by engine_version so the new (Phase A-D) engine's
accuracy is measured CLEAN, never polluted by the 472 pre-fix garbage
signals. Entries with no engine_version = legacy.

Usage:
    python scripts/accuracy_report.py                # all engines, split out
    python scripts/accuracy_report.py --current      # only current engine
    python scripts/accuracy_report.py --since 2026-05-15
"""
import argparse
import json
import os
import statistics as st
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.signal_journal import JOURNAL_FILE, ENGINE_VERSION  # noqa: E402


def _classify(row: dict) -> str:
    """Outcome-first. resolve_signal() writes `outcome`, not `exit_reason`
    — keying off exit_reason (the old bug) reported every closed trade as
    OPEN. Fall back to exit_reason text only when outcome is absent."""
    oc = (row.get("outcome") or "").upper()
    if oc == "TARGET_HIT":
        return "TARGET"
    if oc == "SL_HIT":
        return "SL"
    if oc in ("EXPIRED", "TIME_EXIT"):
        return "TIME"
    r = (row.get("exit_reason") or "").lower()
    if "target" in r:
        return "TARGET"
    if "sl" in r:
        return "SL"
    if "time" in r or "expir" in r:
        return "TIME"
    return "OPEN"


def _stats(rows: list, label: str) -> None:
    closed = [r for r in rows if _classify(r) != "OPEN"]
    if not closed:
        print(f"\n=== {label} ===\n  no closed trades yet "
              f"({len(rows)} signals, awaiting outcomes)")
        return

    c = Counter(_classify(r) for r in closed)
    t, s, tm = c.get("TARGET", 0), c.get("SL", 0), c.get("TIME", 0)
    n = len(closed)
    pn = [r.get("pnl_pct", r.get("pnl_percent"))
          for r in closed if r.get("pnl_pct", r.get("pnl_percent")) is not None]

    print(f"\n=== {label} ===")
    print(f"  closed={n}  (open/untracked={len(rows) - n})")
    print(f"  per 10 trades:  TARGET {10*t/n:.1f}   "
          f"SL {10*s/n:.1f}   TIME {10*tm/n:.1f}")
    if t + s:
        print(f"  target-vs-SL WR = {100*t/(t+s):.0f}%")
    if pn:
        wr = 100 * sum(1 for x in pn if x > 0) / len(pn)
        exp = st.mean(pn) * 100
        print(f"  profitable = {wr:.0f}%   "
              f"expectancy/trade = {exp:+.2f}%   "
              f"median = {st.median(pn)*100:+.2f}%")
        print(f"  best {max(pn)*100:+.1f}%   worst {min(pn)*100:+.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", action="store_true",
                    help="only the current ENGINE_VERSION")
    ap.add_argument("--since", help="ISO date filter, e.g. 2026-05-15")
    args = ap.parse_args()

    if not os.path.exists(JOURNAL_FILE):
        print("no journal file")
        return 1

    rows = []
    for line in open(JOURNAL_FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass

    if args.since:
        rows = [r for r in rows if r.get("ts", "")[:10] >= args.since]

    current = [r for r in rows if r.get("engine_version") == ENGINE_VERSION]
    legacy = [r for r in rows if r.get("engine_version") != ENGINE_VERSION]

    print(f"Journal: {len(rows)} signals  "
          f"(current={len(current)}  legacy={len(legacy)})")
    print(f"Current engine: {ENGINE_VERSION}")

    _stats(current, f"CURRENT ENGINE  {ENGINE_VERSION}")
    if not args.current and legacy:
        _stats(legacy, "LEGACY (pre-fix, ignore for tuning)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
