"""
repair_expired_marks.py — one-time journal repair (2026-07-04 audit).

353 EXPIRED rows written by the OLD tracker (pre-June hardening; identifiable
by missing `exit_reason`) carry impossible premium marks — avg +79%, with 116
rows above +100% (e.g. GAIL PE +135% held 2.6 minutes while spot moved AGAINST
the trade). Root cause: entry premium and exit premium came from inconsistent
pricing sources in the old code. The current tracker path is honest (its
EXPIRED rows average −26%); this script remarks the legacy rows with the SAME
method the current code uses:

    exit_prem = entry_prem + |delta| × favorable_spot_move   (floor 5% of entry)
    minus flat spread (6% of entry) + theta (1.2%/h held)     [core/signal_tracker]

Backup: logs/signal_journal.pre_rollfix.jsonl. Old marks preserved per-row
under `rollfix`.

    python scripts/repair_expired_marks.py
"""
import json
import os
import shutil

import pandas as pd

JOURNAL = os.path.join("logs", "signal_journal.jsonl")
BACKUP = os.path.join("logs", "signal_journal.pre_rollfix.jsonl")

# flat cost fallbacks — mirror core/signal_tracker.py
SPREAD_RT = 0.06
THETA_PER_H = 0.012
FLOOR = 0.05


def main() -> int:
    rows = [json.loads(l) for l in open(JOURNAL, encoding="utf-8") if l.strip()]
    if not os.path.exists(BACKUP):
        shutil.copy(JOURNAL, BACKUP)
        print(f"backup -> {BACKUP}")

    fixed = flagged = 0
    for r in rows:
        if r.get("outcome") != "EXPIRED" or r.get("exit_reason") is not None:
            continue  # current-code rows are already honest
        old = {"old_exit_prem": r.get("exit_prem"), "old_pnl_pct": r.get("pnl_pct"),
               "old_pnl_rupees": r.get("pnl_rupees")}
        try:
            entry = float(r["entry_prem"])
            delta = abs(float(r.get("delta") or 0))
            spot_entry = float(r.get("entry_price") or 0)
            spot_pct = r.get("spot_pnl_pct")
            if not (entry > 0 and spot_entry > 0 and spot_pct is not None):
                raise ValueError("missing fields")
            # favorable spot move in points (spot_pnl_pct is direction-signed)
            move_pts = float(spot_pct) / 100.0 * spot_entry
            gross = max(entry + delta * move_pts, entry * FLOOR)
            try:
                held_h = (pd.to_datetime(r["exit_ts"]) - pd.to_datetime(r["ts"])
                          ).total_seconds() / 3600.0
            except Exception:
                held_h = 0.0
            net = gross - entry * SPREAD_RT - entry * THETA_PER_H * max(held_h, 0.0)
            net = max(net, entry * FLOOR)
            new_pct = round((net - entry) / entry * 100, 2)
            new_rs = old["old_pnl_rupees"]
            try:
                oe, ors = old["old_exit_prem"], old["old_pnl_rupees"]
                if ors is not None and oe is not None and abs(float(oe) - entry) > 1e-9:
                    lot = float(ors) / (float(oe) - entry)
                    new_rs = round((net - entry) * lot, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            r["exit_prem"] = round(net, 2)
            r["pnl_pct"] = new_pct
            r["pnl_rupees"] = new_rs
            r["exit_reason"] = "legacy_mark_repaired_delta_map"
            r["rollfix"] = old
            fixed += 1
        except Exception:
            # can't reconstruct — null the poisoned numbers so nothing trains on them
            r["exit_prem"] = None
            r["pnl_pct"] = None
            r["pnl_rupees"] = None
            r["exit_reason"] = "legacy_mark_unrepairable"
            r["rollfix"] = old
            flagged += 1

    with open(JOURNAL, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"repaired {fixed}, nulled {flagged} unrepairable, total {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
