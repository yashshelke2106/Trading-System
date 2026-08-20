"""A funded book must behave like a book, not like a sum of returns.

The display this replaces multiplied each trade's return by a Rs 50,000
notional. That has no pot: nothing is debited, so a position the account could
never have afforded pays out exactly like one it could, and two positions can
spend the same rupee. These tests pin the properties that make it a ledger.
"""

import pytest

from core.paper_portfolio import (
    Sleeve, _run, build_equity_orders, build_option_orders, build_portfolios,
    OPTION_BROKERAGE_PER_LEG, OPTION_SPREAD_PCT_PER_LEG,
)


def order(sym="X", entry="2026-08-01", exit="2026-08-02", basis=50_000.0,
          pnl=0.0, costs=0.0, **kw):
    o = {
        "symbol": sym, "direction": "LONG", "entry_date": entry, "exit_date": exit,
        "quantity": 1, "entry_price": 100.0, "exit_price": 100.0,
        "cost_basis": basis, "pnl": pnl, "costs": costs,
        "outcome": "MOM_EXIT", "instrument": "equity", "detail": "",
    }
    o.update(kw)
    return o


# ── the ledger identity ──────────────────────────────────────────────────────

def test_equity_is_capital_plus_realised_pnl():
    s = _run(Sleeve("t", 100_000.0), [order(basis=10_000, pnl=500)])
    assert s.realized_pnl == pytest.approx(500)
    assert s.equity == pytest.approx(100_500)


def test_cash_returns_to_the_pot_on_close():
    """Cost basis out at entry, cost basis + P&L back at exit."""
    s = _run(Sleeve("t", 100_000.0), [order(basis=10_000, pnl=-2_000)])
    assert s.cash == pytest.approx(98_000)
    assert s.equity == pytest.approx(98_000)


# ── capital is a real constraint ─────────────────────────────────────────────

def test_a_trade_the_sleeve_cannot_afford_is_skipped_not_paid():
    """The whole point. A notional multiplier would have paid this out."""
    s = _run(Sleeve("t", 10_000.0), [order(basis=50_000, pnl=9_999)])
    assert s.n_taken == 0
    assert s.skipped_no_cash == 1
    assert s.realized_pnl == 0.0
    assert s.equity == pytest.approx(10_000)


def test_two_positions_cannot_spend_the_same_rupee():
    """Both overlap in time; the pot funds one."""
    s = _run(Sleeve("t", 60_000.0), [
        order("A", "2026-08-01", "2026-08-10", basis=50_000, pnl=1_000),
        order("B", "2026-08-02", "2026-08-11", basis=50_000, pnl=1_000),
    ])
    assert s.n_taken == 1
    assert s.skipped_no_cash == 1


def test_capital_frees_up_and_is_reusable():
    """Sequential trades on one slot: both fund off the same money."""
    s = _run(Sleeve("t", 60_000.0), [
        order("A", "2026-08-01", "2026-08-05", basis=50_000, pnl=1_000),
        order("B", "2026-08-06", "2026-08-10", basis=50_000, pnl=1_000),
    ])
    assert s.n_taken == 2
    assert s.skipped_no_cash == 0
    assert s.realized_pnl == pytest.approx(2_000)


def test_same_day_close_frees_capital_before_the_next_open():
    """A desk closes then re-deploys. Ordering opens first would fake a
    capital shortage that never happened."""
    s = _run(Sleeve("t", 50_000.0), [
        order("A", "2026-08-01", "2026-08-05", basis=50_000, pnl=500),
        order("B", "2026-08-05", "2026-08-09", basis=50_000, pnl=500),
    ])
    assert s.n_taken == 2, "same-day recycle must be allowed"


def test_peak_deployed_tracks_concurrent_exposure():
    s = _run(Sleeve("t", 100_000.0), [
        order("A", "2026-08-01", "2026-08-10", basis=30_000),
        order("B", "2026-08-02", "2026-08-10", basis=30_000),
    ])
    assert s.peak_deployed == pytest.approx(60_000)


# ── drawdown ─────────────────────────────────────────────────────────────────

def test_drawdown_measured_from_the_peak_not_from_the_start():
    s = _run(Sleeve("t", 100_000.0), [
        order("A", "2026-08-01", "2026-08-02", basis=10_000, pnl=+10_000),
        order("B", "2026-08-03", "2026-08-04", basis=10_000, pnl=-11_000),
    ])
    # peak 110,000 -> trough 99,000  => -10.0%
    assert s.peak_equity == pytest.approx(110_000)
    assert s.max_drawdown_pct == pytest.approx(-10.0, abs=0.01)


# ── sleeve isolation ─────────────────────────────────────────────────────────

def test_sleeves_do_not_share_cash():
    p = build_portfolios(
        equity_capital=100_000.0, options_capital=100_000.0,
        swing_resolved=[], journal_rows=[],
    )
    assert p["equity"]["equity"] == pytest.approx(100_000)
    assert p["options"]["equity"] == pytest.approx(100_000)
    assert p["combined"]["starting_capital"] == pytest.approx(200_000)


def test_combined_is_a_sum_never_a_blend():
    p = build_portfolios(
        equity_capital=100_000.0, options_capital=100_000.0,
        swing_resolved=[], journal_rows=[],
    )
    c, e, o = p["combined"], p["equity"], p["options"]
    assert c["realized_pnl"] == pytest.approx(e["realized_pnl"] + o["realized_pnl"])


# ── order construction ───────────────────────────────────────────────────────

def test_equity_sizing_fits_whole_shares_into_the_slot():
    rows = [{"symbol": "A", "direction": "long", "signal_date": "2026-08-01",
             "exit_date": "2026-08-02", "entry_px": 3_000.0, "exit_px": 3_030.0,
             "ret_gross": 0.01, "ret_net": 0.01, "outcome": "MOM_EXIT"}]
    o = build_equity_orders(rows, capital=500_000.0, position_pct=0.10)[0]
    assert o["quantity"] == 16                      # 50,000 // 3,000
    assert o["cost_basis"] == pytest.approx(48_000)


def test_equity_pnl_uses_ret_net_and_does_not_double_charge_costs():
    """ret_net already carries the swing book's round trip."""
    rows = [{"symbol": "A", "direction": "long", "signal_date": "2026-08-01",
             "exit_date": "2026-08-02", "entry_px": 100.0, "exit_px": 102.0,
             "ret_gross": 0.02, "ret_net": 0.0175, "outcome": "MOM_EXIT"}]
    o = build_equity_orders(rows, capital=500_000.0, position_pct=0.10)[0]
    assert o["pnl"] == pytest.approx(o["cost_basis"] * 0.0175)
    assert o["costs"] == pytest.approx(o["cost_basis"] * 0.0025)


def test_equity_skips_a_share_pricier_than_the_whole_slot():
    rows = [{"symbol": "PRICEY", "direction": "long", "signal_date": "2026-08-01",
             "exit_date": "2026-08-02", "entry_px": 90_000.0, "exit_px": 91_000.0,
             "ret_gross": 0.01, "ret_net": 0.01, "outcome": "MOM_EXIT"}]
    assert build_equity_orders(rows, capital=500_000.0, position_pct=0.10) == []


def test_option_order_costs_a_full_lot_of_premium(monkeypatch):
    import core.paper_portfolio as pp
    monkeypatch.setattr(pp, "_lot_for", lambda s: 375)
    rows = [{"symbol": "A", "direction": "long", "outcome": "SL_HIT",
             "ts": "2026-08-01T10:00:00", "exit_ts": "2026-08-01T15:00:00",
             "entry_prem": 30.0, "exit_prem": 18.0,
             "option_strike": 2300.0, "option_type": "CE"}]
    o = pp.build_option_orders(rows)[0]
    assert o["quantity"] == 375
    assert o["cost_basis"] == pytest.approx(30.0 * 375)
    expected_costs = (30.0 + 18.0) * 375 * OPTION_SPREAD_PCT_PER_LEG \
                     + 2 * OPTION_BROKERAGE_PER_LEG
    assert o["costs"] == pytest.approx(expected_costs)
    assert o["pnl"] == pytest.approx((18.0 - 30.0) * 375 - expected_costs)


def test_option_with_unknown_lot_is_not_sized(monkeypatch):
    """An unresolved contract size must not be silently traded as 1 share."""
    import core.paper_portfolio as pp
    monkeypatch.setattr(pp, "_lot_for", lambda s: 1)
    rows = [{"symbol": "MCDOWELL-N", "direction": "long", "outcome": "SL_HIT",
             "ts": "2026-08-01T10:00:00", "exit_ts": "2026-08-01T15:00:00",
             "entry_prem": 30.0, "exit_prem": 18.0}]
    assert pp.build_option_orders(rows) == []


def test_open_positions_are_not_counted_as_closed(monkeypatch):
    import core.paper_portfolio as pp
    monkeypatch.setattr(pp, "_lot_for", lambda s: 100)
    rows = [{"symbol": "A", "direction": "long", "outcome": None,
             "ts": "2026-08-01T10:00:00", "exit_ts": None,
             "entry_prem": 30.0, "exit_prem": None}]
    assert pp.build_option_orders(rows) == []
