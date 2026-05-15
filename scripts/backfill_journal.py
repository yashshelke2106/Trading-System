"""
One-time backfill: re-resolve every signal_journal.jsonl entry that has
pnl_pct=None or exit_price==entry_price using exit_replay.replay_exit.

Writes patched journal to logs/signal_journal_backfilled.jsonl. Diff first,
then rename to replace original.

Run:
    python scripts/backfill_journal.py --dry-run
    python scripts/backfill_journal.py --apply
"""

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.exit_replay import replay_exit  # noqa: E402

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
SRC = os.path.join(LOG_DIR, "signal_journal.jsonl")
DST = os.path.join(LOG_DIR, "signal_journal_backfilled.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show summary, don't write")
    ap.add_argument("--apply", action="store_true", help="overwrite original journal")
    args = ap.parse_args()

    if not os.path.exists(SRC):
        print(f"Source journal not found: {SRC}")
        return 1

    with open(SRC, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]

    print(f"Loaded {len(entries)} journal entries")

    patched = []
    stats = Counter()
    needs_fix = 0

    for e in entries:
        ep = float(e.get("entry_price") or 0)
        xp = float(e.get("exit_price") or 0)
        pnl = e.get("pnl_pct")
        # Targets for backfill: pnl=None OR exit_price==entry_price stub
        if pnl is None or (ep > 0 and abs(ep - xp) < 1e-6):
            needs_fix += 1
            sig = {
                "symbol":       e.get("symbol"),
                "direction":    e.get("direction", "long"),
                "entry_price":  ep,
                "sl_price":     e.get("sl_price"),
                "target_price": e.get("target_price"),
                "ts":           e.get("ts") or e.get("ts_signal"),
            }
            res = replay_exit(sig)
            stats[res["outcome"]] += 1
            new_e = dict(e)
            # Only overwrite if replay succeeded (NO_DATA leaves entry as-is)
            if res["outcome"] != "NO_DATA":
                new_e["outcome"]    = res["outcome"]
                new_e["exit_price"] = res["exit_price"]
                new_e["pnl_pct"]    = res["pnl_pct"]
                new_e["mfe_pct"]    = res["mfe_pct"]
                new_e["mae_pct"]    = res["mae_pct"]
                new_e["exit_reason"] = res["exit_reason"]
                new_e["backfilled"] = True
            patched.append(new_e)
        else:
            stats["UNCHANGED"] += 1
            patched.append(e)

    print(f"\nEntries needing fix: {needs_fix}")
    print("Outcome distribution after backfill:")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    if args.dry_run:
        print("\n[dry-run] no files written")
        return 0

    with open(DST, "w", encoding="utf-8") as f:
        for e in patched:
            f.write(json.dumps(e, default=str) + "\n")
    print(f"\nWrote backfilled journal: {DST}")

    if args.apply:
        backup = SRC + ".pre_backfill.bak"
        os.replace(SRC, backup)
        os.replace(DST, SRC)
        print(f"Original backed up to {backup}")
        print(f"Replaced {SRC} with backfilled version")
    else:
        print("Run with --apply to replace the original journal")

    return 0


if __name__ == "__main__":
    sys.exit(main())
