"""
One-command daily update for the event/news system.

Runs, in order:
  1. news snapshot        - append today's full news feed to logs/news_archive.jsonl
  2. reaction collect     - typed per-symbol news events (full F&O universe)
  3. reaction analyze     - fill forward returns for ripe events, print report
  4. events refresh       - re-pull corporate_actions if archive older than
                            REFRESH_DAYS (future event dates go stale otherwise)

Safe to run any time; every step is idempotent/deduped. Schedule daily ~16:00
IST (after close). One command:

    python -m scripts.events_update
    python -m scripts.events_update --force-events   # refresh events regardless of age
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REFRESH_DAYS = 7  # corporate-actions archive considered stale after this


def _events_age_days() -> float:
    from scripts.build_events_archive import MANIFEST
    if not os.path.exists(MANIFEST):
        return 1e9
    try:
        built = json.load(open(MANIFEST, encoding="utf-8")).get("built_at")
        if not built:
            return 1e9
        return (datetime.now() - datetime.fromisoformat(built)).total_seconds() / 86400
    except Exception:
        return 1e9


def main(argv) -> int:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Daily event/news system update")
    p.add_argument("--force-events", action="store_true",
                   help="refresh corporate-actions archive regardless of age")
    p.add_argument("--skip-collect", action="store_true",
                   help="skip per-symbol reaction collect (snapshot+analyze only)")
    args = p.parse_args(argv)

    from core import news_reaction as nr
    from core.universe import FO_UNIVERSE

    print("=" * 60)
    print(f"EVENTS UPDATE  {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 60)

    # 1. news snapshot (forward archive - every skipped day is lost forever)
    n = nr.snapshot_news()
    print(f"[1/4] news snapshot: +{n} new article(s)")

    # 2. per-symbol typed reaction rows (feed is cached, so this is 1 HTTP call)
    if args.skip_collect:
        print("[2/4] collect: skipped (--skip-collect)")
    else:
        c = nr.collect(list(FO_UNIVERSE))
        print(f"[2/4] collect: +{c} reaction event(s)")

    # 3. fill forward returns for ripe events
    print("[3/4] analyze:")
    nr.analyze()

    # 4. weekly corporate-actions refresh (exact future dates go stale)
    age = _events_age_days()
    if args.force_events or age > REFRESH_DAYS:
        print(f"[4/4] events archive refresh (age {age:.1f}d > {REFRESH_DAYS}d)...")
        from scripts.build_events_archive import build
        build(list(FO_UNIVERSE), sleep=1.5, refresh=True, probe=False)
    else:
        print(f"[4/4] events archive fresh ({age:.1f}d old) - skipped")

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
