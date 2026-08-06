"""Position sizing runs in one order, and each step can veto with a reason.

Sizing logic used to be spread across execution.py, risk_engine.py and the
agents, and the pieces disagreed -- two read a stale lot map (34 of 62 entries
wrong, 81 names missing) while a third read the live scrip master, and none
asked whether the account could fund the position at all.

The order is the invariant these tests protect:
    liquidity -> fundable -> live lot size -> risk budget -> breach
"""

import pytest

from core.sizing import decide


def test_illiquid_name_is_vetoed_before_anything_else():
    d = decide("PNB", 110.0, 107.0, capital=2_000_000)
    assert not d.ok and d.quantity == 0
    assert "liquidity floor" in d.reason


def test_unfundable_position_is_vetoed_with_the_shortfall_named():
    d = decide("RELIANCE", 1325.0, 1298.0, capital=100_000)
    assert not d.ok and d.quantity == 0
    assert "unfundable" in d.reason
    assert d.margin_required > 100_000


def test_a_fundable_liquid_trade_sizes_in_whole_lots():
    d = decide("RELIANCE", 1325.0, 1298.0, capital=2_000_000)
    assert d.ok and d.quantity > 0
    assert d.quantity % d.lot_size == 0
    assert d.lots == d.quantity // d.lot_size


def test_lot_size_comes_from_the_live_scrip_master():
    """KOTAKBANK is 2000 live and 400 in the stale static map."""
    d = decide("KOTAKBANK", 396.0, 388.0, capital=2_000_000)
    assert d.ok
    assert d.lot_size == 2000, "must not fall back to config.NSE_LOT_SIZES"


def test_risk_breach_is_reported_but_still_sized_by_default():
    """Current policy: one lot is taken even when it exceeds the cap."""
    d = decide("RELIANCE", 1325.0, 1298.0, capital=100_000,
               instrument="option_long", premium=30.55)
    assert d.ok and d.quantity > 0
    assert d.risk_breach_multiple and d.risk_breach_multiple > 1
    assert "OVER BUDGET" in d.reason


def test_risk_breach_can_be_refused_explicitly():
    """Refusing changes which trades you take, so it is opt-in."""
    d = decide("RELIANCE", 1325.0, 1298.0, capital=100_000,
               instrument="option_long", premium=30.55,
               allow_risk_breach=False)
    assert not d.ok and d.quantity == 0
    assert "over" in d.reason.lower()


def test_actual_risk_matches_quantity_times_stop_distance():
    d = decide("RELIANCE", 1325.0, 1298.0, capital=2_000_000)
    assert d.actual_risk == pytest.approx(abs(1325.0 - 1298.0) * d.quantity,
                                          rel=1e-6)


def test_zero_stop_distance_is_rejected_not_divided_by():
    d = decide("RELIANCE", 1325.0, 1325.0, capital=2_000_000)
    assert not d.ok and d.quantity == 0
    assert "zero" in d.reason


def test_non_positive_capital_or_price_is_rejected():
    assert not decide("RELIANCE", 0.0, 10.0, capital=1e6).ok
    assert not decide("RELIANCE", 100.0, 90.0, capital=0).ok


def test_liquidity_check_can_be_bypassed_for_research():
    d = decide("PNB", 110.0, 107.0, capital=2_000_000,
               require_liquidity=False)
    assert d.ok and d.quantity > 0


def test_decision_is_serialisable():
    d = decide("RELIANCE", 1325.0, 1298.0, capital=2_000_000).to_dict()
    assert d["symbol"] == "RELIANCE" and d["ok"] is True
    assert "reason" in d and "margin_required" in d


def test_a_veto_always_carries_a_reason():
    """Silence at the top of a funnel is indistinguishable from no signal."""
    for d in (decide("PNB", 110.0, 107.0, capital=2_000_000),
              decide("RELIANCE", 1325.0, 1298.0, capital=100_000),
              decide("RELIANCE", 100.0, 100.0, capital=1e6)):
        assert not d.ok
        assert d.reason and d.reason != "ok"
