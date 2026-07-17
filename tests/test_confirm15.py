"""Evals for the first-15m confirmation collector (forward evidence only)."""
import sys, os
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swing_tracker import annotate_confirmations, first15_confirms


def _bars(day, o, c):
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} 09:15"),
                            pd.Timestamp(f"{day} 09:30")])
    return pd.DataFrame({"open": [o, c], "high": [max(o, c)] * 2,
                         "low": [min(o, c)] * 2, "close": [c, c]}, index=idx)


def test_confirm_direction_logic():
    assert first15_confirms("long", 100.0, 101.0) is True
    assert first15_confirms("long", 100.0, 99.0) is False
    assert first15_confirms("short", 100.0, 99.0) is True
    assert first15_confirms("short", 100.0, 101.0) is False


def test_annotation_uses_first_session_after_signal():
    rows = [{"symbol": "AAA", "direction": "long", "signal_date": "2026-07-14",
             "status": "open"}]
    n = annotate_confirmations(rows, intraday={"AAA": _bars("2026-07-15", 100.0, 102.0)})
    assert n == 1
    assert rows[0]["confirm_15m"] is True
    assert rows[0]["first15_open"] == 100.0 and rows[0]["first15_close"] == 102.0


def test_annotation_skips_when_entry_day_not_in_window():
    # intraday data older than the signal -> no entry-day bars -> untouched
    rows = [{"symbol": "AAA", "direction": "long", "signal_date": "2026-07-14",
             "status": "open"}]
    n = annotate_confirmations(rows, intraday={"AAA": _bars("2026-07-10", 100, 99)})
    assert n == 0 and "confirm_15m" not in rows[0]


def test_annotation_idempotent():
    rows = [{"symbol": "AAA", "direction": "short", "signal_date": "2026-07-14",
             "status": "open", "confirm_15m": False}]
    n = annotate_confirmations(rows, intraday={"AAA": _bars("2026-07-15", 100, 90)})
    assert n == 0 and rows[0]["confirm_15m"] is False   # already stamped, unchanged
