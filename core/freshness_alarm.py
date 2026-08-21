"""
freshness_alarm.py — the thing nobody was watching.

WHY THIS EXISTS
---------------
On 2026-08-20 the EOD archive was 6 trading days stale and one intraday
symbol had drifted to 27 of a 60-day recovery window before anyone noticed.
capture_task.py ran on schedule and reported "OK" every time — the RUN
succeeded, the DATA was decaying, and nothing distinguished those two
states out loud. Past 60 days, missing 5-minute history is not late, it is
gone: yfinance/Dhan only carry that lookback, so a lapse of that length is
unrecoverable, not merely inconvenient.

This module is the missing "and tell someone" step. It reads the same
freshness numbers /api/capture already exposes, compares them to two
thresholds, and fires through the existing TelegramNotifier (core/notifier.py)
-- which degrades gracefully to file-only mode if Telegram isn't configured,
so this alarm requires zero new setup to start working.

THRESHOLDS
----------
  EOD:      WARN  if the last archived trading day is > EOD_WARN_DAYS old
  Intraday: WARN  if worst_stale_days > INTRADAY_WARN_DAYS (approaching the cliff)
            CRIT  if worst_stale_days > MAX_LOOKBACK_DAYS (already unrecoverable)

RATE LIMITING
-------------
An ongoing outage should not spam Telegram once per scheduled run. Each alert
KIND is throttled independently via logs/freshness_alarm_state.json, so a
already-notified problem re-alerts only every ALERT_COOLDOWN_HOURS, while a
NEW kind of problem (or a WARN escalating to CRIT) always fires immediately.

WIRING
------
Called automatically at the end of capture_task.run() -- see the call there --
so it fires on the schedule you already have (TradingSystem_MarketCapture,
daily >=16:00 IST). No new scheduled task needed.

    python -m core.freshness_alarm            # check now, using the last capture run
    python -m core.freshness_alarm --force    # ignore cooldown, always notify if breached
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_FILE = os.path.join(_ROOT, "logs", "freshness_alarm_state.json")

EOD_WARN_DAYS = 2          # a working system updates daily; 2 covers a weekend
INTRADAY_WARN_DAYS = 45    # 15 days of runway left before the 60d cliff
ALERT_COOLDOWN_HOURS = 20  # re-alert on a persisting problem at most once/day


@dataclass
class Alert:
    kind: str              # stable id, used for throttling ("eod_stale", "intraday_warn", ...)
    severity: str          # "WARN" | "CRIT"
    message: str
    detail: Dict = field(default_factory=dict)


def _today() -> date:
    return datetime.now().date()


def _compute_eod_archive() -> Dict:
    """Same computation /api/capture uses. capture_task.load_status() does NOT
    carry this -- its `layers.eod` records what THIS run appended, not the
    archive's current last-bar date, so relying on it would silently never
    catch EOD staleness through the default (no-argument) call path."""
    from pathlib import Path
    try:
        daily_dir = Path(_ROOT) / "logs" / "bhavcopy_archive" / "daily"
        days = sorted(p.stem for p in daily_dir.glob("*.parquet"))
        return {"trading_days": len(days), "last": days[-1] if days else None}
    except Exception as e:
        return {"error": str(e)}


def _eod_staleness(eod_archive: Dict) -> Optional[int]:
    last = eod_archive.get("last")
    if not last:
        return None
    try:
        last_date = datetime.strptime(str(last), "%Y%m%d").date()
    except ValueError:
        try:
            last_date = date.fromisoformat(str(last))
        except ValueError:
            return None
    return (_today() - last_date).days


def evaluate(capture_status: Dict) -> List[Alert]:
    """Pure function: capture payload -> alerts. No I/O, easy to test."""
    alerts: List[Alert] = []

    eod = capture_status.get("eod_archive") or {}
    if eod.get("error"):
        alerts.append(Alert("eod_error", "CRIT",
                            f"EOD archive unreadable: {eod['error']}", eod))
    else:
        age = _eod_staleness(eod)
        if age is not None and age > EOD_WARN_DAYS:
            alerts.append(Alert(
                "eod_stale", "WARN",
                f"EOD archive is {age}d stale (last bar {eod.get('last')}). "
                f"capture_task.py's eod leg is not keeping up.",
                {"age_days": age, "last": eod.get("last")}))

    intr = capture_status.get("intraday") or capture_status.get("intraday_coverage") or {}
    if intr.get("error"):
        alerts.append(Alert("intraday_error", "CRIT",
                            f"intraday capture unreadable: {intr['error']}", intr))
    else:
        worst = intr.get("worst_stale_days")
        limit = intr.get("limit_days", 60)
        if worst is not None:
            if intr.get("unrecoverable") or worst > limit:
                alerts.append(Alert(
                    "intraday_crit", "CRIT",
                    f"Intraday 5m history is being LOST: worst symbol is "
                    f"{worst}d stale against a {limit}d recovery window. "
                    f"Those sessions cannot be backfilled.",
                    {"worst_stale_days": worst, "limit_days": limit}))
            elif worst > INTRADAY_WARN_DAYS:
                alerts.append(Alert(
                    "intraday_warn", "WARN",
                    f"Intraday coverage is aging toward the cliff: worst "
                    f"symbol {worst}d stale of a {limit}d limit "
                    f"({limit - worst}d of runway left).",
                    {"worst_stale_days": worst, "limit_days": limit}))

    return alerts


# ── Throttling + notification ────────────────────────────────────────────────

def _load_throttle_state() -> Dict[str, str]:
    if not os.path.exists(_STATE_FILE):
        return {}
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_throttle_state(state: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _past_cooldown(kind: str, severity: str, state: Dict[str, str], force: bool) -> bool:
    if force:
        return True
    last_key = f"{kind}:{severity}"
    last = state.get(last_key)
    if last is None:
        return True
    try:
        elapsed_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
    except ValueError:
        return True
    return elapsed_h >= ALERT_COOLDOWN_HOURS


def check_and_notify(capture_status: Optional[Dict] = None, *,
                     force: bool = False, quiet: bool = False) -> List[Alert]:
    """Evaluate freshness and notify (throttled) for anything breached.

    Returns the alerts that were EVALUATED (breached), regardless of whether
    the throttle suppressed the actual notification -- so a caller can still
    see "there is a live problem" even between cooldown-suppressed pings.
    """
    if capture_status is None:
        from capture_task import load_status
        st = load_status()
        capture_status = {
            "eod_archive": _compute_eod_archive(),
            "intraday": st.get("intraday_coverage") or {},
        }

    alerts = evaluate(capture_status)
    if not alerts:
        if not quiet:
            print("[freshness_alarm] all clear")
        return alerts

    state = _load_throttle_state()
    try:
        from core.notifier import TelegramNotifier
        notifier = TelegramNotifier()
    except Exception as e:
        notifier = None
        if not quiet:
            print(f"[freshness_alarm] notifier unavailable ({e}); printing only")

    now_iso = datetime.now().isoformat(timespec="seconds")
    for a in alerts:
        fire = _past_cooldown(a.kind, a.severity, state, force)
        prefix = "ALARM" if fire else "throttled"
        if not quiet:
            print(f"[freshness_alarm] {a.severity} [{prefix}] {a.message}")
        if fire:
            if notifier is not None:
                notifier.notify(f"DATA_FRESHNESS_{a.severity}", a.message)
            state[f"{a.kind}:{a.severity}"] = now_iso

    _save_throttle_state(state)
    return alerts


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="ignore the cooldown, notify now if anything is breached")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    alerts = check_and_notify(force=args.force, quiet=args.quiet)
    return 1 if any(a.severity == "CRIT" for a in alerts) else 0


if __name__ == "__main__":
    sys.exit(main())
