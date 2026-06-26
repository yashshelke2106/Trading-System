"""Tests for core/pead_study.py — PEAD harness. Synthetic, deterministic
(no dependence on the live archive)."""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from core.pead_study import run_pead, _holding_return, _norm_bars

DATES = pd.bdate_range("2026-01-01", periods=30)
ANNOUNCE = DATES[10]
# entry_lag=1 -> entry at idx 11; hold_days=5 -> exit at idx 16
ENTRY_IDX, EXIT_IDX = 11, 16


def _mk(entry_px=100.0, exit_px=100.0, vol=1e8):
    close = [100.0] * len(DATES)
    close[ENTRY_IDX] = entry_px
    close[EXIT_IDX] = exit_px
    return pd.DataFrame({"date": DATES, "close": close, "volume": [vol] * len(DATES)})


def _flat_benchmark():
    return _norm_bars(pd.DataFrame({"date": DATES, "close": [100.0] * len(DATES),
                                    "volume": [1e9] * len(DATES)}))


def test_holding_return_enters_after_announcement():
    df = _norm_bars(_mk(entry_px=100, exit_px=108))
    e_date, x_date, raw = _holding_return(df, ANNOUNCE, entry_lag=1, hold_days=5)
    assert e_date == DATES[ENTRY_IDX]          # entered the bar AFTER announce
    assert x_date == DATES[EXIT_IDX]
    assert raw == pytest.approx(0.08, rel=1e-9)


def test_market_adjustment_subtracts_benchmark():
    bars = {"S": _mk(entry_px=100, exit_px=110)}            # +10% raw
    bench = _flat_benchmark()
    bench.loc[bench["date"] == DATES[EXIT_IDX], "close"] = 104.0   # benchmark +4%
    ev = pd.DataFrame([{"symbol": "S", "date": ANNOUNCE, "surprise_pct": 5.0}])
    r = run_pead(ev, get_bars=lambda s: bars.get(s), benchmark=bench,
                 entry_lag=1, hold_days=5)
    assert r.n_events == 1
    row = r.events.iloc[0]
    assert row["raw_ret"] == pytest.approx(0.10, rel=1e-6)
    assert row["mkt_adj_ret"] == pytest.approx(0.06, abs=1e-6)   # 10% - 4%


def test_cost_netting_and_tier():
    bars = {"S": _mk(entry_px=100, exit_px=100)}   # turnover 100*1e8/1e7 = 1000 Cr -> 0.20% cost
    ev = pd.DataFrame([{"symbol": "S", "date": ANNOUNCE, "surprise_pct": 5.0}])
    r = run_pead(ev, get_bars=lambda s: bars.get(s), benchmark=_flat_benchmark(),
                 entry_lag=1, hold_days=5)
    row = r.events.iloc[0]
    assert row["cost"] == pytest.approx(0.0020, abs=1e-9)
    assert row["net_long_ret"] == pytest.approx(row["mkt_adj_ret"] - 0.0020, abs=1e-9)


def test_beat_miss_spread():
    """6+ events split into terciles; beats drift up, misses down → positive
    gross spread; net spread = gross − 2×cost."""
    bars, rows = {}, []
    # 3 beats (+8% raw, high surprise), 3 misses (-5% raw, low surprise)
    for i in range(3):
        bars[f"BEAT{i}"] = _mk(entry_px=100, exit_px=108)
        rows.append({"symbol": f"BEAT{i}", "date": ANNOUNCE, "surprise_pct": 8.0 + i})
        bars[f"MISS{i}"] = _mk(entry_px=100, exit_px=95)
        rows.append({"symbol": f"MISS{i}", "date": ANNOUNCE, "surprise_pct": -8.0 - i})
    ev = pd.DataFrame(rows)
    r = run_pead(ev, get_bars=lambda s: bars.get(s), benchmark=_flat_benchmark(),
                 entry_lag=1, hold_days=5)
    assert r.n_events == 6
    assert r.by_tercile["beat"]["mean_mkt_adj"] == pytest.approx(0.08, abs=1e-6)
    assert r.by_tercile["miss"]["mean_mkt_adj"] == pytest.approx(-0.05, abs=1e-6)
    assert r.spread_gross == pytest.approx(0.13, abs=1e-6)
    # net = gross − 2×mean cost (cost 0.0020 each name)
    assert r.spread_net == pytest.approx(0.13 - 2 * 0.0020, abs=1e-6)


def test_insufficient_forward_bars_skipped():
    bars = {"S": _mk()}
    # announce near the very end → no room for entry_lag+hold_days bars
    ev = pd.DataFrame([{"symbol": "S", "date": DATES[28], "surprise_pct": 5.0}])
    r = run_pead(ev, get_bars=lambda s: bars.get(s), benchmark=_flat_benchmark(),
                 entry_lag=1, hold_days=20)
    assert r.n_events == 0


def test_distinct_dates_counted_for_power():
    bars = {f"S{i}": _mk(exit_px=108) for i in range(4)}
    # 4 events but only 2 distinct announce dates → independence unit = 2
    rows = [
        {"symbol": "S0", "date": ANNOUNCE, "surprise_pct": 5},
        {"symbol": "S1", "date": ANNOUNCE, "surprise_pct": 5},
        {"symbol": "S2", "date": DATES[9], "surprise_pct": 5},
        {"symbol": "S3", "date": DATES[9], "surprise_pct": 5},
    ]
    r = run_pead(pd.DataFrame(rows), get_bars=lambda s: bars.get(s),
                 benchmark=_flat_benchmark(), entry_lag=1, hold_days=5)
    assert r.n_distinct_dates == 2
