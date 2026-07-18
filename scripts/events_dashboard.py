"""
Event/news system dashboard - read-only terminal view.

Shows:
  1. RISK FLAGS today       - symbols with results/board meeting within 2 days
                              (these get skip_entry in the scan pipeline)
  2. EVENT CALENDAR (7d)    - all dated corporate events coming up
  3. NEWS ARCHIVE           - size + latest capture
  4. REACTION LOG           - collected typed events, ripe vs pending
  5. STUDY VERDICT          - latest event_study result + stat-gate disposition

Read-only: touches no file, calls no API. One command:

    python -m scripts.events_dashboard
    python -m scripts.events_dashboard --days 14
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_ARCHIVE = os.path.join(_ROOT, "logs", "news_archive.jsonl")
REACTION_LOG = os.path.join(_ROOT, "logs", "news_reaction.jsonl")
STUDY_RESULTS = os.path.join(_ROOT, "logs", "event_study_results.json")


def _jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Event/news dashboard (read-only)")
    p.add_argument("--days", type=int, default=7, help="calendar horizon (days)")
    args = p.parse_args(argv)

    from core.news_filter import EventCalendar
    ec = EventCalendar()
    today = date.today()

    print("=" * 66)
    print(f"EVENT DASHBOARD  {today}   (archive: {len(ec.exact_events)} symbols "
          f"with future events)")
    print("=" * 66)

    # 1. risk flags - gap events within 2d => skip_entry in pipeline
    flagged = set()
    upcoming = set()
    for sym, evs in ec.exact_events.items():
        for ev_date, ev_type in evs:
            d = (ev_date - today).days
            if d > args.days:
                break
            upcoming.add((d, ev_date, sym, ev_type))
            if d <= 2 and ev_type in ec.GAP_RISK_EVENTS:
                flagged.add((d, sym, ev_type))
    flagged = sorted(flagged)
    upcoming = sorted(upcoming)

    print(f"\nRISK FLAGS (skip_entry - results/board mtg within 2d): "
          f"{len(flagged)}")
    for d, sym, et in flagged:
        when = "TODAY" if d == 0 else f"+{d}d"
        print(f"  [X] {sym:14s} {et:14s} {when}")
    if not flagged:
        print("  (none)")

    # 2. calendar
    print(f"\nEVENT CALENDAR next {args.days}d: {len(upcoming)} events")
    for d, ev_date, sym, et in upcoming[:30]:
        mark = "!" if et in ec.GAP_RISK_EVENTS else " "
        print(f"  {mark} {ev_date}  +{d}d  {sym:14s} {et}")
    if len(upcoming) > 30:
        print(f"  ... and {len(upcoming) - 30} more")

    # 3. news archive
    arch = _jsonl(NEWS_ARCHIVE)
    latest = max((r.get("captured_at", "") for r in arch), default="-")
    print(f"\nNEWS ARCHIVE: {len(arch)} articles   latest capture: {latest[:16]}")

    # 4. reaction log
    rx = _jsonl(REACTION_LOG)
    ripe = sum(1 for r in rx if r.get("ripe"))
    by_sig = Counter(r.get("signal") for r in rx)
    print(f"REACTION LOG: {len(rx)} events   ripe: {ripe}   pending: {len(rx) - ripe}"
          f"   (bull {by_sig.get('bullish', 0)} / bear {by_sig.get('bearish', 0)})")

    # 5. study verdict
    print("\nSTUDY VERDICT:")
    if os.path.exists(STUDY_RESULTS):
        res = json.load(open(STUDY_RESULTS, encoding="utf-8"))
        used = res.get("events_used", "?")
        print(f"  retrospective study: {used} events, metric={res.get('metric', '?')}")
    print("  stat gate (2026-07-18): REJECTED - no event type passes clustered")
    print("  bootstrap + Bonferroni + cost. Events are RISK CONTEXT, not signals.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
