"""Tests for core/smallcap_costs.py — turnover-tiered cost model."""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from core.smallcap_costs import (
    ILLIQUID_TAIL_COST,
    MIN_ROUNDTRIP_COST,
    compute_avg_daily_turnover_cr,
    cost_tier_label,
    estimate_roundtrip_cost,
    net_edge_after_cost,
)


def test_monotonic_less_liquid_costs_more():
    """Cost must be non-increasing in turnover (less liquid = higher cost)."""
    turnovers = [5, 50, 150, 500, 2000]
    costs = [estimate_roundtrip_cost("X", t) for t in turnovers]
    assert costs == sorted(costs, reverse=True)
    # and strictly higher at the illiquid end than the liquid end
    assert costs[0] > costs[-1]


def test_known_tier_values():
    assert estimate_roundtrip_cost("X", 1500) == 0.0020   # largecap
    assert estimate_roundtrip_cost("X", 500) == 0.0050    # liquid midcap (DIXON-tier)
    assert estimate_roundtrip_cost("X", 150) == 0.0100    # midcap
    assert estimate_roundtrip_cost("X", 50) == 0.0200     # small-mid illiquid
    assert estimate_roundtrip_cost("X", 5) == ILLIQUID_TAIL_COST  # very illiquid


def test_floor_and_unknown():
    # never below the absolute floor
    assert estimate_roundtrip_cost("X", 1e9) >= MIN_ROUNDTRIP_COST
    # unknown / zero / negative turnover → most conservative (illiquid tail)
    assert estimate_roundtrip_cost("X", 0) == ILLIQUID_TAIL_COST
    assert estimate_roundtrip_cost("X", -1) == ILLIQUID_TAIL_COST
    assert estimate_roundtrip_cost("X", None) == ILLIQUID_TAIL_COST


def test_tier_labels():
    assert cost_tier_label(1500) == "largecap_liquid"
    assert cost_tier_label(500) == "midcap_liquid"
    assert cost_tier_label(5) == "very_illiquid_smallcap"
    assert cost_tier_label(0) == "unknown_illiquid"


def test_edge_survives_on_liquid_dies_on_illiquid():
    """The whole point: a 3-5% PEAD edge survives on a liquid midcap but is
    eaten on an illiquid smallcap."""
    for gross in (0.03, 0.04, 0.05):
        liquid = net_edge_after_cost(gross, avg_daily_turnover_cr=500)   # 0.50% cost
        illiquid = net_edge_after_cost(gross, avg_daily_turnover_cr=5)   # 4.00% cost
        assert liquid > 0.02, f"edge {gross} should clearly survive on liquid midcap"
        assert illiquid < liquid
    # a 3% edge is net-negative or marginal on a very illiquid name
    assert net_edge_after_cost(0.03, avg_daily_turnover_cr=5) < 0.0


def test_compute_turnover_cr():
    # 100,000 shares/day at price 200 = 2 Cr/day turnover
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=70, freq="D"),
        "close": [200.0] * 70,
        "volume": [100_000] * 70,
    })
    t = compute_avg_daily_turnover_cr(df, window=60)
    assert t == pytest.approx(2.0, rel=1e-6)


def test_compute_turnover_pointintime_no_lookahead():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=10, freq="D"),
        "close": [100.0] * 10,
        "volume": [10_000] * 5 + [10_000_000] * 5,  # liquidity explodes later
    })
    # asof before the explosion must NOT see the later high-volume bars
    early = compute_avg_daily_turnover_cr(df, window=60, asof="2026-01-05")
    late = compute_avg_daily_turnover_cr(df, window=60)
    assert late > early


def test_empty_bars():
    assert compute_avg_daily_turnover_cr(pd.DataFrame()) == 0.0
