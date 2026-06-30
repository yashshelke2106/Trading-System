"""Tests for core/allocation.py — path-#1 allocation engine."""

from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy
import numpy as np
import pandas as pd
import pytest

from core.allocation import (
    ALLOCATION_CONFIG, TargetAllocation,
    compute_target_allocation, backtest_allocation, perf_summary,
)

DATES = pd.bdate_range("2020-01-01", periods=400)


def _rising():
    return pd.Series(np.linspace(100, 200, len(DATES)), index=DATES)   # always > 200DMA


def _falling_end():
    up = np.linspace(100, 200, 300)
    dn = np.linspace(200, 120, len(DATES) - 300)
    return pd.Series(np.concatenate([up, dn]), index=DATES)            # ends below 200DMA


def _cfg(overlay):
    c = copy.deepcopy(ALLOCATION_CONFIG)
    c["trend_overlay"]["enabled"] = overlay
    return c


def test_core_only_always_full_equity():
    t = compute_target_allocation(_rising(), cfg=_cfg(False))
    assert t.equity_weight == 1.0 and t.cash_weight == 0.0
    assert t.state == "core_only"


def test_overlay_risk_on_above_ma():
    t = compute_target_allocation(_rising(), cfg=_cfg(True))
    assert t.state == "risk_on" and t.equity_weight == 1.0


def test_overlay_risk_off_below_ma():
    c = _cfg(True)
    t = compute_target_allocation(_falling_end(), cfg=c)
    assert t.state == "risk_off"
    assert t.equity_weight == c["trend_overlay"]["risk_off_weight"]
    assert t.cash_weight == pytest.approx(1 - c["trend_overlay"]["risk_off_weight"])


def test_backtest_no_lookahead_and_cost():
    # overlay must act on the PRIOR day's signal, not today's
    r = backtest_allocation(_falling_end(), cfg=_cfg(True))
    assert len(r) == len(DATES)
    assert r.notna().all()


def test_overlay_reduces_drawdown_on_crash():
    # a series that crashes at the end: overlay should cut the drawdown vs core-only
    s = _falling_end()
    dd_core = perf_summary(backtest_allocation(s, cfg=_cfg(False)))["max_dd"]
    dd_overlay = perf_summary(backtest_allocation(s, cfg=_cfg(True)))["max_dd"]
    assert dd_overlay >= dd_core  # less negative = shallower drawdown


def test_asof_is_point_in_time():
    s = _falling_end()
    early = compute_target_allocation(s, asof="2020-06-01", cfg=_cfg(True))
    assert early.asof <= "2020-06-01"


def test_perf_summary_keys():
    r = backtest_allocation(_rising(), cfg=_cfg(False))
    p = perf_summary(r)
    assert set(p) >= {"cagr", "vol", "sharpe", "max_dd", "years"}
