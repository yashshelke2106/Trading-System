"""
System health — data-staleness gate + alerting.

With yfinance removed, Dhan is the single data source. If it goes silent
mid-session the system must HALT, not trade on stale/empty data (that's how
you fire orders into a void). This module exposes:

  data_ok(threshold_sec) -> (ok, seconds_since)
      False when no successful Dhan fetch in `threshold_sec` during a live
      session. The scanner calls this before emitting and skips the cycle
      (and alerts once) when data is stale.

  alert(event, message, symbol="", pnl=0.0)
      Fire-and-forget notification (file + Telegram if configured) via the
      existing core.notifier. Deduped per event-key for STALE so we don't
      spam every scan cycle while data is down.

Used by scan_only_v2 (pre-scan staleness gate) and metrics_writer
(drift/redesign alerts).
"""

from __future__ import annotations

import logging
import time
from typing import Tuple

log = logging.getLogger(__name__)

# Default: halt if no fresh data for 5 minutes in a live session.
STALE_THRESHOLD_SEC = 300

_last_alert: dict = {}          # event_key -> ts, for dedupe
_ALERT_DEDUPE_SEC = 600         # don't repeat the same alert within 10 min


def data_ok(threshold_sec: int = STALE_THRESHOLD_SEC) -> Tuple[bool, float]:
    """Is Dhan data fresh? Returns (ok, seconds_since_last_fetch)."""
    try:
        from core.api_dhan import seconds_since_last_data
        secs = seconds_since_last_data()
    except Exception:
        return True, 0.0       # can't tell -> don't block (degrade open)
    return (secs <= threshold_sec), secs


def alert(event: str, message: str, symbol: str = "", pnl: float = 0.0,
          dedupe: bool = False) -> None:
    """Send an alert (file + Telegram). dedupe=True suppresses repeats of the
    same event within _ALERT_DEDUPE_SEC (use for recurring conditions)."""
    if dedupe:
        now = time.time()
        last = _last_alert.get(event, 0.0)
        if now - last < _ALERT_DEDUPE_SEC:
            return
        _last_alert[event] = now
    try:
        from core.notifier import TelegramNotifier
        TelegramNotifier().notify(event, message, symbol, pnl)
    except Exception as e:
        log.debug(f"[Health] alert send failed: {e}")
    log.warning(f"[ALERT:{event}] {message}")


def check_data_or_alert(threshold_sec: int = STALE_THRESHOLD_SEC) -> bool:
    """Convenience for the scanner: returns True if data is fresh; if stale,
    fires a deduped STALE alert and returns False so the caller can skip the
    cycle."""
    ok, secs = data_ok(threshold_sec)
    if not ok:
        alert("DATA_STALE",
              f"⚠️ Dhan data STALE — no fresh bars for {secs/60:.1f} min. "
              f"Scanning halted. Check Dhan API / subscription.",
              dedupe=True)
    return ok
