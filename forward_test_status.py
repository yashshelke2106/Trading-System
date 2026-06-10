"""
Forward paper-test tracker — the clean OUT-OF-SAMPLE test the system never had.

All prior edge tests were on history (overfittable). This scores ONLY signals
generated AFTER you start the forward run, through the same honest-metrics gate
that refuses to show a mirage. First run stamps the start date; later runs report
forward-only performance.

  python forward_test_status.py            # show status (stamps start on first run)
  python forward_test_status.py --reset    # restart the forward window from today
"""
from __future__ import annotations
import os, sys, argparse
from datetime import datetime, date

try:                                  # Windows cp1252 chokes on → in output
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
MARKER = os.path.join("logs", "forward_test_start.txt")
JOURNAL = os.path.join("logs", "signal_journal.jsonl")


def _count_since(since: str):
    import json
    total = resolved = 0
    if os.path.exists(JOURNAL):
        for line in open(JOURNAL, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if str(r.get("ts", "")) >= since:
                total += 1
                if r.get("spot_outcome") or r.get("outcome") or r.get("exit_ts"):
                    resolved += 1
    return total, resolved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    os.makedirs("logs", exist_ok=True)

    if args.reset or not os.path.exists(MARKER):
        start = date.today().isoformat()
        with open(MARKER, "w") as f:
            f.write(start)
        print(f"[forward-test] start date stamped: {start}")
    start = open(MARKER).read().strip()

    days = (date.today() - date.fromisoformat(start)).days
    total, resolved = _count_since(start)

    print("=" * 64)
    print(f"  FORWARD PAPER TEST — started {start}  ({days} days ago)")
    print("=" * 64)
    print(f"  forward signals journaled : {total}")
    print(f"  resolved (outcome known)  : {resolved}")

    from core.honest_performance import from_journal
    p = from_journal(since=start)
    print("-" * 64)
    if not p.trustworthy:
        print(f"  honest verdict: NOT TRUSTWORTHY YET")
        print(f"  {p.note}")
        print(f"\n  → keep the run going; need more resolved spot outcomes before any")
        print(f"    forward number means anything. (This is the gate doing its job.)")
    else:
        d = p.as_dict()
        print(f"  honest verdict: TRUSTWORTHY  (n_clean={d.get('n_clean')})")
        print(f"  win_rate     : {d.get('win_rate')}")
        print(f"  profit_factor: {d.get('profit_factor')}")
        print(f"  expectancy   : {d.get('expectancy_pct')}% / signal  (spot, cost-free)")
        print(f"  note         : {d.get('note')}")
        for a in p.alarms:
            print(f"  ALARM: {a}")
        exp = d.get("expectancy_pct") or 0
        print("-" * 64)
        if exp > 0:
            print("  → POSITIVE forward expectancy. This is the only edge signal that")
            print("    can't be curve-fit. Cross-verify before believing it, but worth it.")
        else:
            print("  → forward expectancy <= 0, consistent with the no-edge research.")
    print("=" * 64)
    print("  Run the scanner during market hours to accumulate signals:")
    print("    start_trading.bat        (UI at http://localhost:3000)")
    print("    or:  python scan_only_v2.py")
    print("  Money meanwhile: index/factor funds (step 2 — yours to action).")


if __name__ == "__main__":
    main()
