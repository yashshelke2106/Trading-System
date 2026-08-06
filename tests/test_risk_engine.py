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
    """P&L is reported NET of charges; gross_pnl and charges are kept so the
    two reconcile against a broker contract note."""
    r = RiskEngine(capital=100000)
    r.positions = [Position("X", "long", 100, 10, 95, 110)]
    tr = r.close_position("X", 110, reason="target")
    assert tr is not None
    assert abs(tr.gross_pnl - 100) < 1e-6
    assert tr.charges > 0
    assert abs(tr.pnl - (tr.gross_pnl - tr.charges)) < 1e-6
    assert tr.pnl < tr.gross_pnl
    assert tr.status == "WIN"


# ── GAP #2: entry gate must block when open-MTM pushes combined loss over cap ──

def test_validate_trade_blocked_by_open_mtm():
    """GAP #2: execute_trade calls validate_trade with mark_prices.
    An open position that has lost enough to breach the daily cap should cause
    validate_trade to set is_valid=False even when realized P&L is zero."""
    from core.risk_engine import RiskEngine, Position

    r = RiskEngine(capital=100000)
    # Plant an open position that is 5% underwater (> 4% max_daily_loss cap).
    r.positions = [Position("RELIANCE", "long", 1000, 100, 950, 1100)]
    marks = {"RELIANCE": 950}   # -50/share × 100 shares = -5000 (5% of capital)

    # Without marks: realized-only check passes (daily_pnl=0).
    candidate = Position("TCS", "long", 3000, 10, 2900, 3200)
    result_no_marks = r.validate_trade(candidate, 3000, force_allowed=True)
    assert result_no_marks['is_valid'] is True, (
        "Without marks the legacy realized-only path should still pass"
    )

    # With marks: MTM loss of 5000 / 100000 = 5% > 4% cap → must block.
    result_with_marks = r.validate_trade(candidate, 3000, force_allowed=True,
                                         mark_prices=marks)
    assert result_with_marks['is_valid'] is False, (
        "With marks the entry gate must block when open MTM breaches daily cap"
    )
    assert any("loss" in r.lower() for r in result_with_marks['reasons']), (
        "Block reason should mention daily loss"
    )


# ── GAP #4: drawdown halt stops new trades after peak-to-trough breach ────────

def test_drawdown_halt_stops_new_trades():
    """GAP #4: can_trade() must return False when intraday peak-to-trough
    drawdown exceeds max_drawdown_halt. Drawdown is measured vs capital, not
    just realized. Validate that it halts AND that a smaller drawdown does not."""
    from core.risk_engine import RiskEngine, Position
    import config as cfg

    halt_ratio = cfg.RISK_CONFIG.get('max_drawdown_halt', 0.08)
    r = RiskEngine(capital=100000)

    # Simulate: account ran up to +4000, then fell to -5000 → drawdown = 9000
    # which is 9% of 100k, above the 8% halt threshold.
    r.daily_pnl = 4000.0
    r._session_peak_equity = 4000.0   # set peak manually (as if it was reached earlier)
    r.daily_pnl = -5000.0             # now session is negative; peak still 4000

    # Without open positions, marks don't matter for this scenario.
    assert r.check_drawdown_halt() is False, (
        "Drawdown of 9% should trigger halt (threshold is 8%)"
    )
    assert r.can_trade(force_allowed=True) is False, (
        "can_trade() must return False when drawdown halt fires"
    )

    # Small drawdown: peak=500, current=100 → drawdown=400=0.4% < 8% → allow.
    r2 = RiskEngine(capital=100000)
    r2.daily_pnl = 500.0
    r2._session_peak_equity = 500.0
    r2.daily_pnl = 100.0
    assert r2.check_drawdown_halt() is True, (
        "Drawdown of 0.4% should NOT trigger halt"
    )
    assert r2.can_trade(force_allowed=True) is True


# ── GAP #3: concurrent-positions ceiling and per-expiry concentration ─────────

def test_concurrent_positions_cap_blocks_entry():
    """GAP #3: validate_trade must block when adding one more position would
    exceed max_concurrent_positions. Confirm the cap fires on the *next* entry
    when the book is already full, and that the reason string is present."""
    from core.risk_engine import RiskEngine, Position

    r = RiskEngine(capital=100000)
    # Override the cap to 2 for this test — the config default is 5.
    r.config = {**r.config, 'max_concurrent_positions': 2}

    # Fill the book to the cap.
    r.positions = [
        Position("RELIANCE", "long", 2500, 50, 2450, 2600),
        Position("TCS",      "long", 3500, 20, 3400, 3700),
    ]
    assert len(r.positions) == 2   # at cap

    candidate = Position("INFY", "long", 1500, 30, 1450, 1600)
    result = r.validate_trade(candidate, 1500, force_allowed=True)

    assert result['is_valid'] is False, (
        "Entry must be blocked when max_concurrent_positions is reached"
    )
    assert any("concurrent" in reason.lower() for reason in result['reasons']), (
        "Reason must mention concurrent positions"
    )


def test_concurrent_positions_zero_disables_cap():
    """GAP #3: setting max_concurrent_positions=0 must disable the cap — a
    full book should still pass the check."""
    from core.risk_engine import RiskEngine, Position

    r = RiskEngine(capital=100000)
    r.config = {**r.config, 'max_concurrent_positions': 0}

    r.positions = [Position(f"SYM{i}", "long", 100, 10, 95, 110) for i in range(10)]
    assert r.check_concurrent_positions() is True, (
        "Cap disabled (0) must always return True regardless of book size"
    )


def test_expiry_concentration_cap_blocks_entry():
    """GAP #3: validate_trade must block when adding a position would push
    the count of positions on the same expiry above max_per_expiry."""
    from core.risk_engine import RiskEngine, Position

    r = RiskEngine(capital=100000)
    # Override the cap to 2 for this test — the config default is 3.
    r.config = {**r.config, 'max_per_expiry': 2}

    expiry = "2026-06-26"
    r.positions = [
        Position("NIFTY",     "long", 24000, 75, 23800, 24400, option_expiry=expiry),
        Position("BANKNIFTY", "long", 51000, 35, 50500, 52000, option_expiry=expiry),
    ]
    # Book has 2 positions on `expiry` — at the cap; a third must be blocked.
    candidate = Position("FINNIFTY", "long", 23000, 65, 22700, 23600,
                         option_expiry=expiry)
    result = r.validate_trade(candidate, 23000, force_allowed=True)

    assert result['is_valid'] is False, (
        "Entry must be blocked when max_per_expiry is reached for that expiry"
    )
    assert any("expiry" in reason.lower() for reason in result['reasons']), (
        "Reason must mention expiry"
    )


def test_expiry_concentration_zero_disables_cap():
    """GAP #3: setting max_per_expiry=0 must disable the cap. A position on
    the same expiry should pass through unchecked."""
    from core.risk_engine import RiskEngine, Position

    r = RiskEngine(capital=100000)
    r.config = {**r.config, 'max_per_expiry': 0}

    expiry = "2026-06-26"
    r.positions = [Position(f"S{i}", "long", 100, 10, 95, 110, option_expiry=expiry)
                   for i in range(5)]

    assert r.check_expiry_concentration(expiry) is True, (
        "Cap disabled (0) must always return True"
    )


def test_expiry_blank_never_blocked():
    """GAP #3: a position with no expiry (futures/equity) must never be blocked
    by the expiry concentration cap regardless of how many blank-expiry
    positions are already open."""
    from core.risk_engine import RiskEngine, Position

    r = RiskEngine(capital=100000)
    r.config = {**r.config, 'max_per_expiry': 1}   # very tight cap

    # Five futures positions — all blank expiry.
    r.positions = [Position(f"F{i}", "long", 100, 10, 95, 110, option_expiry="")
                   for i in range(5)]

    assert r.check_expiry_concentration("") is True, (
        "Blank expiry must always pass — futures/equity have no expiry to cap"
    )
