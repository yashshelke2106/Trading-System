"""Tests for core.risk_engine — position sizing, stops, the daily kill-switch
(incl. open MTM), and the correlation cap. Money-critical paths."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.risk_engine import RiskEngine, Position


def test_quantity_basic_no_lot():
    r = RiskEngine(capital=100000)
    # 1.2% of 100k = 1200 risk; risk/share = 5 -> 240 shares; symbol='' -> lot 1
    assert r.calculate_quantity(100000, 100, 95, symbol="") == 240


def test_quantity_zero_risk_returns_zero():
    r = RiskEngine(capital=100000)
    assert r.calculate_quantity(100000, 100, 100, symbol="") == 0


def test_quantity_lot_rounding(monkeypatch):
    import core.futures_leg as fl
    monkeypatch.setattr(fl, "lot_size_for", lambda s: 250)
    r = RiskEngine(capital=100000)
    # raw qty (24) < 1 lot -> floored UP to one lot (250)
    q = r.calculate_quantity(100000, 2500, 2450, symbol="RELIANCE")
    assert q == 250 and q % 250 == 0


def test_stop_loss_long_below_entry():
    r = RiskEngine(capital=100000)
    sl = r.calculate_stop_loss(100, "long", atr=2.0)
    assert sl < 100 and 97.0 <= sl <= 99.5


def test_stop_loss_short_above_entry():
    r = RiskEngine(capital=100000)
    assert r.calculate_stop_loss(100, "short", atr=2.0) > 100


def test_stop_loss_min_floor():
    r = RiskEngine(capital=100000)            # tiny ATR -> 1% floor -> sl 99
    assert abs(r.calculate_stop_loss(100, "long", atr=0.01) - 99.0) < 0.01


def test_stop_loss_max_ceiling():
    r = RiskEngine(capital=100000)            # huge ATR -> 2.5% cap -> sl 97.5
    assert abs(r.calculate_stop_loss(100, "long", atr=100) - 97.5) < 0.01


def test_target_rr_long_and_short():
    r = RiskEngine(capital=100000)
    assert abs(r.calculate_target(100, "long", 95, min_rr=1.5) - 107.5) < 1e-6
    assert abs(r.calculate_target(100, "short", 105, min_rr=1.5) - 92.5) < 1e-6


def test_daily_loss_includes_open_mtm():
    r = RiskEngine(capital=100000)
    r.positions = [Position("X", "long", 100, 1000, 95, 110)]
    marks = {"X": 90}                          # -10% open loss > 4% cap
    assert abs(r.unrealized_pnl(marks) - (-10000)) < 1e-6
    assert r.check_daily_loss() is True         # realized-only (legacy) -> ok
    assert r.check_daily_loss(marks) is False   # MTM breach -> halt


def test_correlation_cap():
    r = RiskEngine(capital=100000)
    sm = {"A": "bank", "B": "bank", "C": "it"}
    r.positions = [Position("A", "long", 1, 1, 1, 1),
                   Position("B", "long", 1, 1, 1, 1)]
    assert r.check_correlation("D", sector_map={"D": "bank", **sm}) is False
    assert r.check_correlation("E", sector_map={"E": "it", **sm}) is True


def test_consecutive_and_trade_limits():
    r = RiskEngine(capital=100000)
    assert r.check_consecutive_losses() is True
    r.consecutive_losses = 2
    assert r.check_consecutive_losses() is False
    r.trades_today = 3
    assert r.check_trades_limit() is False


def test_close_position_pnl_win():
    r = RiskEngine(capital=100000)
    r.positions = [Position("X", "long", 100, 10, 95, 110)]
    tr = r.close_position("X", 110, reason="target")
    assert tr is not None and abs(tr.pnl - 100) < 1e-6 and tr.status == "WIN"
