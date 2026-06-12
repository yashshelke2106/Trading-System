"""Tests for the gap-honest exit engine (backtest_live_pipeline.simulate_exit).
The key property: a bar that OPENS through the stop fills at the OPEN (worse
than the stop), not at the stop price — the optimism the old harness hid."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from backtest_live_pipeline import simulate_exit


def _mk(bars):
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"])


def test_long_normal_stop_fills_at_stop():
    df = _mk([(100, 100, 100, 100), (99, 99.5, 94, 96)])
    out = simulate_exit(df, 0, 100, 95, 110, "long", be_trail_r=99)
    assert out[0] == "SL" and abs(out[1] - 95) < 1e-9 and out[3] is False


def test_long_gap_down_through_stop_fills_at_open():
    df = _mk([(100, 100, 100, 100), (90, 92, 88, 89)])
    out = simulate_exit(df, 0, 100, 95, 110, "long", be_trail_r=99)
    assert out[0] == "SL" and abs(out[1] - 90) < 1e-9 and out[3] is True


def test_long_target_gap_fills_at_open():
    df = _mk([(100, 100, 100, 100), (112, 113, 111, 112)])
    out = simulate_exit(df, 0, 100, 95, 110, "long", be_trail_r=99)
    assert out[0] == "TARGET" and abs(out[1] - 112) < 1e-9


def test_long_breakeven_trail():
    df = _mk([(100, 100, 100, 100), (101, 104, 100.5, 103), (101, 102, 99, 99.5)])
    out = simulate_exit(df, 0, 100, 95, 120, "long", be_trail_r=0.7)
    assert out[0] == "BE_STOP" and abs(out[1] - 100) < 1e-9


def test_short_gap_up_through_stop_fills_at_open():
    df = _mk([(100, 100, 100, 100), (110, 112, 108, 111)])
    out = simulate_exit(df, 0, 100, 105, 90, "short", be_trail_r=99)
    assert out[0] == "SL" and abs(out[1] - 110) < 1e-9 and out[3] is True


def test_time_exit():
    df = _mk([(100, 100, 100, 100), (100, 101, 99.5, 100.2), (100, 101, 99.6, 100.4)])
    out = simulate_exit(df, 0, 100, 95, 120, "long", hold=2, be_trail_r=99)
    assert out[0] == "TIME_EXIT"
