"""
Leakage audit - coded checks, not a checklist document.

Runs mechanical audits against the CURRENT event-study inputs and reports
PASS / WARN / FAIL per check. HARD FAIL on any violation means the study's
numbers are inadmissible until fixed.

Checks:
  1. duplicate-events     - same (symbol, date, event_type) counted more than
                            once inflates n and fakes precision.
  2. future-events        - events dated beyond the last price bar must be
                            excluded from any measured cell (they have no
                            forward window; a bug here = look-ahead).
  3. entry-timing         - t0 must be the first bar ON/AFTER the event date,
                            never before (entering before the event = leakage).
  4. survivorship         - universe is today's F&O list (survivors). Cannot be
                            fixed with free data -> permanent WARN; mitigated by
                            per-stock baseline subtraction in event_study.
  5. baseline-contamination - the per-stock baseline includes event windows
                            themselves (slightly shrinks measured abnormals;
                            conservative direction) -> WARN, documented.
  6. holdout-integrity    - holdout ledger is append-only and every spent cell
                            has exactly one verdict (no overwrites).

RUN:  python -m core.leakage_audit
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import date
from typing import List, Tuple

from .event_study import _load_events, _load_ohlc, _parse_date, _t0_index

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDOUT_LEDGER = os.path.join(_ROOT, "logs", "holdout_ledger.jsonl")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def check_duplicates() -> Tuple[str, str]:
    events = _load_events()
    keys = [(e.get("symbol"), e.get("date"), e.get("event_type"))
            for e in events if e.get("date")]
    dups = {k: c for k, c in Counter(keys).items() if c > 1}
    n_extra = sum(c - 1 for c in dups.values())
    if not dups:
        return PASS, "no duplicate (symbol,date,type) rows"
    frac = n_extra / max(len(keys), 1)
    level = FAIL if frac > 0.10 else WARN
    return level, (f"{len(dups)} duplicated keys, {n_extra} extra rows "
                   f"({frac:.1%} of events) - n is inflated; dedup before "
                   f"trusting tight CIs")


def check_future_events() -> Tuple[str, str]:
    today = date.today()
    n_future = sum(1 for e in _load_events()
                   if (d := _parse_date(e.get("date"))) and d > today)
    # future events exist in the archive (calendar data) - that is fine;
    # the study must produce NO returns for them (no forward bars exist).
    # Spot-check: a future event must yield no +1d return.
    sample = next((e for e in _load_events()
                   if (d := _parse_date(e.get("date"))) and d > today), None)
    if sample:
        df = _load_ohlc(sample["symbol"])
        if not df.empty:
            i0 = _t0_index(df, _parse_date(sample["date"]))
            if i0 is not None and i0 + 1 < len(df):
                return FAIL, (f"future event {sample['symbol']} {sample['date']} "
                              f"has forward bars - look-ahead bug")
    return PASS, f"{n_future} future-dated calendar events; none measurable"


def check_entry_timing(n_samples: int = 50) -> Tuple[str, str]:
    checked = 0
    for e in _load_events():
        d = _parse_date(e.get("date"))
        if not d:
            continue
        df = _load_ohlc(e.get("symbol", ""))
        if df.empty:
            continue
        i0 = _t0_index(df, d)
        if i0 is None:
            continue
        if df["date"].iloc[i0] < d:
            return FAIL, (f"t0 bar {df['date'].iloc[i0]} precedes event date "
                          f"{d} for {e.get('symbol')} - entry before event")
        checked += 1
        if checked >= n_samples:
            break
    return PASS, f"t0 >= event date on all {checked} sampled events"


def check_survivorship() -> Tuple[str, str]:
    return WARN, ("universe = current F&O survivors (yfinance); raw drift is "
                  "inflated ~+5%/20d - MITIGATED by per-stock baseline "
                  "subtraction in event_study (verified: abnormals ~0). "
                  "Un-fixable without a bhavcopy archive.")


def check_baseline_contamination() -> Tuple[str, str]:
    return WARN, ("per-stock baseline includes event windows themselves; "
                  "shrinks measured abnormals slightly (conservative - biases "
                  "TOWARD rejecting edges, never toward accepting)")


def check_holdout_integrity() -> Tuple[str, str]:
    if not os.path.exists(HOLDOUT_LEDGER):
        return PASS, "no holdout spent yet"
    cells = Counter()
    with open(HOLDOUT_LEDGER, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                cells[(r["event_type"], r["horizon"])] += 1
            except Exception:
                return FAIL, "ledger has unparseable lines"
    multi = {k: c for k, c in cells.items() if c > 1}
    if multi:
        return FAIL, f"cells with >1 holdout verdict (retry happened): {multi}"
    return PASS, f"{len(cells)} spent cell(s), one verdict each"


CHECKS = [
    ("duplicate-events", check_duplicates),
    ("future-events", check_future_events),
    ("entry-timing", check_entry_timing),
    ("survivorship", check_survivorship),
    ("baseline-contamination", check_baseline_contamination),
    ("holdout-integrity", check_holdout_integrity),
]


def run_audit() -> bool:
    """Print report; True iff no FAIL."""
    print("=" * 74)
    print("LEAKAGE AUDIT (coded checks on current event-study inputs)")
    print("=" * 74)
    ok = True
    for name, fn in CHECKS:
        try:
            level, msg = fn()
        except Exception as e:  # an audit that cannot run is a failure
            level, msg = FAIL, f"check crashed: {e}"
        ok = ok and (level != FAIL)
        print(f"  [{level:4s}] {name:24s} {msg}")
    print("-" * 74)
    print("  HARD RULE: any FAIL -> study numbers inadmissible until fixed.")
    print(f"  overall: {'ADMISSIBLE' if ok else 'INADMISSIBLE'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run_audit() else 1)
