"""
Trade mode — ONE switch that changes the system's whole personality
between intraday day-trading and multi-day SWING trading.

Why this exists
---------------
The engine was born intraday: 5m execution / 15m setup / 1H+1D bias,
a 6.5-hour hold horizon, and time-of-day gates (skip the open, block
the afternoon). Swing trading needs the opposite: daily execution,
weekly trend context, a multi-DAY horizon, and NO intraday clock gates
(a swing setup is valid at 14:00 just as much as 10:00).

Rather than rip those assumptions out of a dozen files (a mistake —
irreversible, untestable), every horizon/timeframe assumption now reads
from HERE. Flip MODE and the whole system changes consistently; flip it
back and nothing is lost. Set env TRADE_MODE=intraday to override.

Timeframe slot mapping (slot names kept from the intraday era so the
~400 lines of downstream logic are untouched — they just carry slower
bars in swing mode):

  slot      intraday        swing
  ──────    ─────────       ─────────────────────────
  exec      5m              1d   (signal triggers on daily close)
  setup     15m             1d   (structure SL = daily swing)
  hbias     60m (1H)        1d   (mid-trend confirm)
  trend     1d              1wk  (weekly regime, resampled)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class ModeConfig:
    name: str
    # Each TF slot: ("intraday", minutes) | ("daily",) | ("weekly",)
    exec_tf:  Tuple
    setup_tf: Tuple
    hbias_tf: Tuple
    trend_tf: Tuple
    # History depth to fetch per slot
    intraday_days_back: int
    daily_days_back: int            # also feeds the weekly resample
    # Hold / outcome horizon
    hold_horizon_hours: float       # signal age before EXPIRED
    replay_window_hours: float      # exit_replay walk-forward cap
    replay_bar: str                 # "5m" | "1d" — bar size for the walk
    # Behaviour switches
    bypass_intraday_time_gates: bool   # skip open-noise + afternoon block
    min_days_to_expiry: int            # roll option to next expiry if closer
    recent_trim_days: int              # _recent() keep-window (0 = no trim)


_INTRADAY = ModeConfig(
    name="intraday",
    exec_tf=("intraday", 5),
    setup_tf=("intraday", 15),
    hbias_tf=("intraday", 60),
    trend_tf=("daily",),
    intraday_days_back=5,
    daily_days_back=60,
    hold_horizon_hours=6.5,         # one NSE session
    replay_window_hours=6.5,
    replay_bar="5m",
    bypass_intraday_time_gates=False,
    min_days_to_expiry=2,
    recent_trim_days=10,
)

# Swing: 2–10 trading-day holds. ~7 trading days ≈ 7*6.25h market ≈ 44h,
# but signals rest overnight/weekends — measure the hold in CALENDAR
# hours generously so the tracker doesn't EXPIRE a live swing early.
_SWING = ModeConfig(
    name="swing",
    exec_tf=("daily",),
    setup_tf=("daily",),
    hbias_tf=("daily",),
    trend_tf=("weekly",),
    intraday_days_back=5,           # unused in swing, kept for symmetry
    daily_days_back=300,            # ~300 sessions → deep daily + ~60 weekly
    hold_horizon_hours=24.0 * 10,   # 10 calendar days max swing hold
    replay_window_hours=24.0 * 10,
    replay_bar="1d",                # walk DAILY bars over the swing window
    bypass_intraday_time_gates=True,
    min_days_to_expiry=10,          # swing hold needs real time value left
    recent_trim_days=0,             # NEVER trim daily history (kills indicators)
)

_MODES = {"intraday": _INTRADAY, "swing": _SWING}

# Default = swing (the structurally honest home for this engine, per the
# theta/edge analysis). Override: env TRADE_MODE=intraday.
MODE = os.environ.get("TRADE_MODE", "swing").strip().lower()
if MODE not in _MODES:
    MODE = "swing"


def get_mode() -> ModeConfig:
    """The active mode config. Single source of truth."""
    return _MODES.get(MODE, _SWING)


def is_swing() -> bool:
    return get_mode().name == "swing"


if __name__ == "__main__":
    import json
    m = get_mode()
    print(json.dumps({
        "MODE": MODE,
        "exec_tf": m.exec_tf, "setup_tf": m.setup_tf,
        "hbias_tf": m.hbias_tf, "trend_tf": m.trend_tf,
        "hold_horizon_hours": m.hold_horizon_hours,
        "replay_bar": m.replay_bar,
        "bypass_intraday_time_gates": m.bypass_intraday_time_gates,
        "min_days_to_expiry": m.min_days_to_expiry,
        "daily_days_back": m.daily_days_back,
    }, indent=2, default=str))
