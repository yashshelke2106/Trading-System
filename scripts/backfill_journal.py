"""
Backfill signal_journal.jsonl entries that have exit_price == entry_price
(old EXPIRED stubs with pnl=0 garbage). Replays each through exit_replay
to get the real outcome.

Usage:
    python scripts/backfill_journal.py [--dry-run]
"""

import json
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

JOURNAL = os.path.join(os.path.dirname(__file__), "..", "logs", "signal_journal.jsonl")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, don't write")
    args = parser.parse_args()

    with open(JOURNAL) as f:
        lines = f.readlines()

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))

    # Find backfill candidates: EXPIRED with exit==entry and no spot_outcome
    candidates = []
    for i, e in enumerate(entries):
        if (e.get("outcome") == "EXPIRED"
            and e.get("exit_price") == e.get("entry_price")
            and e.get("exit_price") is not None
            and not (e.get("extra", {}) or {}).get("spot_outcome")):
            candidates.append(i)

    print(f"Total entries: {len(entries)}")
    print(f"Backfill candidates: {len(candidates)}")

    if not candidates:
        print("Nothing to backfill.")
        return

    from core.exit_replay import replay_exit

    updated = 0
    errors = 0
    for idx in candidates:
        e = entries[idx]
        sym = e.get("symbol", "?")
        try:
            res = replay_exit(e)
            if res["outcome"] == "NO_DATA":
                print(f"  {sym}: NO_DATA (skipped)")
                errors += 1
                continue

            # Update entry with real outcome data
            e["exit_price"] = res["exit_price"]
            e["pnl_pct"] = res["pnl_pct"]

            # Only upgrade outcome if replay found a definitive result
            if res["outcome"] in ("TARGET_HIT", "SL_HIT"):
                e["outcome"] = res["outcome"]

            extra = e.get("extra", {}) or {}
            extra.update({
                "spot_outcome": res["outcome"],
                "spot_pnl_pct": res["pnl_pct"],
                "mfe_pct": res["mfe_pct"],
                "mae_pct": res["mae_pct"],
                "exit_reason": res["exit_reason"],
                "backfilled": True,
                "backfill_ts": datetime.now().isoformat(),
            })
            e["extra"] = extra

            direction = e.get("direction", "long")
            print(f"  {sym} {direction}: {res['outcome']} "
                  f"pnl={res['pnl_pct']:+.2f}% "
                  f"mfe={res['mfe_pct']:+.2f}% "
                  f"exit={res['exit_price']:.2f}")
            updated += 1
        except Exception as ex:
            print(f"  {sym}: ERROR {ex}")
            errors += 1

    print(f"\nUpdated: {updated}, Errors: {errors}")

    if args.dry_run:
        print("DRY RUN -- no file written.")
        return

    # Write back
    backup = JOURNAL + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.rename(JOURNAL, backup)
    print(f"Backup: {backup}")

    with open(JOURNAL, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    print(f"Written: {JOURNAL}")


if __name__ == "__main__":
    main()
