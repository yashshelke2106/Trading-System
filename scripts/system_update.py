"""
FULL system update - every data pipeline in one command.

Order:
  1. price archive   - 10yr OHLC (yfinance). Auto-refreshes ALL symbols when the
                       archive is >3 calendar days stale (otherwise only fills
                       missing symbols - fast no-op).
  2. events archive  - corporate_actions re-pull when older than 7 days
                       (future event dates go stale; risk flags depend on them).
  3. news snapshot   - append today's feed to the forward news archive.
  4. reaction collect- typed per-symbol news events, full F&O universe.
  5. reaction analyze- fill forward returns for ripe events.
  6. event study     - refresh logs/event_study_results.json (dashboard reads it).

NOT included: core/event_stat_gate (5000-draw bootstrap, slow) - run monthly by
hand if the study numbers move.

Everything is idempotent/deduped - safe to run any time. Schedule daily ~16:00
IST after close:

    system_update.bat            (or: python -m scripts.system_update)
    system_update.bat --fast     (skip price-archive refresh even if stale)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICE_STALE_DAYS = 3
EVENTS_STALE_DAYS = 7


def _price_archive_age_days() -> float:
    """Days since the newest bar in the RELIANCE archive CSV (proxy for all)."""
    csv = os.path.join(_ROOT, "data", "history", "RELIANCE.csv")
    if not os.path.exists(csv):
        return 1e9
    try:
        import pandas as pd
        last = pd.read_csv(csv)["date"].iloc[-1]
        return (date.today() - datetime.strptime(last[:10], "%Y-%m-%d").date()).days
    except Exception:
        return 1e9


def _events_age_days() -> float:
    from scripts.build_events_archive import MANIFEST
    if not os.path.exists(MANIFEST):
        return 1e9
    try:
        built = json.load(open(MANIFEST, encoding="utf-8")).get("built_at")
        return ((datetime.now() - datetime.fromisoformat(built)).total_seconds() / 86400
                if built else 1e9)
    except Exception:
        return 1e9


def main(argv) -> int:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Full system data update")
    p.add_argument("--fast", action="store_true",
                   help="skip price-archive refresh even if stale")
    p.add_argument("--force-events", action="store_true")
    args = p.parse_args(argv)

    from core import news_reaction as nr
    from core.universe import FO_UNIVERSE

    print("=" * 62)
    print(f"SYSTEM UPDATE  {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 62)

    # 1. price archive (weekends make 2d gaps normal; refresh past 3d)
    age = _price_archive_age_days()
    from scripts.build_history_archive import build as build_prices
    if args.fast:
        print(f"[1/6] price archive: skipped (--fast; {age:.0f}d stale)")
    elif age > PRICE_STALE_DAYS:
        print(f"[1/6] price archive: {age:.0f}d stale -> refreshing all...")
        build_prices(list(FO_UNIVERSE), "10yr", sleep=0.2,
                     refresh=True, probe=False, source="yfinance")
    else:
        print(f"[1/6] price archive: fresh ({age:.0f}d) - top-up only")
        build_prices(list(FO_UNIVERSE), "10yr", sleep=0.2,
                     refresh=False, probe=False, source="yfinance")

    # 2. events archive
    eage = _events_age_days()
    if args.force_events or eage > EVENTS_STALE_DAYS:
        print(f"[2/6] events archive: {eage:.1f}d old -> refreshing...")
        from scripts.build_events_archive import build as build_events
        build_events(list(FO_UNIVERSE), sleep=1.5, refresh=True, probe=False)
    else:
        print(f"[2/6] events archive: fresh ({eage:.1f}d) - skipped")

    # 3. news snapshot
    n = nr.snapshot_news()
    print(f"[3/6] news snapshot: +{n} article(s)")

    # 4. typed reaction collect (feed cached -> 1 HTTP call)
    c = nr.collect(list(FO_UNIVERSE))
    print(f"[4/6] reaction collect: +{c} event(s)")

    # 5. analyze forward returns
    print("[5/6] reaction analyze:")
    nr.analyze()

    # 6. refresh study results for the dashboard
    print("[6/6] event study (refreshing results json):")
    from core.event_study import run as study_run
    study_run(min_n=20)

    print("\nsystem update done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
