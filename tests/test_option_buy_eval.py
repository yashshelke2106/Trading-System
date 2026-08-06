"""Option-buy evaluation: does delta out-earn theta?

Buying an option to ride momentum is a race between delta (pays when the
underlying moves your way) and theta (charges you daily regardless). The
decisive quantity is the drift required just to break even:

    required_daily_move = |theta_per_day| / delta

Measured live 2026-08-06: RELIANCE ATM 19 DTE needs ~0.100%/day, while the
whole market offers ~0.0982%/day (beta 0.0773% + the entire, non-significant
momentum excess 0.0209%) -- before option spreads. That is the mechanism
behind the PF 0.44 already measured for buying premium.

These tests use a synthetic chain so they pin the ARITHMETIC and the guard
rails, not live market values.
"""

import pytest

from core.option_buy_eval import evaluate_buy
from datetime import date

ASOF = date(2026, 8, 6)


def _chain(spot=1325.0, expiry="2026-08-25", ltp=30.55, iv=20.0):
    return [{"strike": 1300.0 + 10 * i, "ce_ltp": ltp, "pe_ltp": ltp,
             "ce_iv": iv, "pe_iv": iv, "_spot": spot, "_expiry": expiry}
            for i in range(6)]


def test_required_move_is_theta_over_delta():
    e = evaluate_buy("RELIANCE", "CE", chain=_chain(), asof=ASOF)
    assert e.required_daily_move == pytest.approx(
        abs(e.theta_per_day) / e.delta, rel=1e-3)
    assert e.required_daily_move_pct == pytest.approx(
        e.required_daily_move / e.spot * 100, rel=1e-3)


def test_theta_is_reported_as_a_daily_loss():
    e = evaluate_buy("RELIANCE", "CE", chain=_chain(), asof=ASOF)
    assert e.theta_per_day < 0, "theta must be a cost to a long option"
    assert 0 < e.theta_pct_of_premium_per_day < 100


def test_expected_move_below_the_hurdle_is_rejected():
    e = evaluate_buy("RELIANCE", "CE", chain=_chain(),
                     expected_daily_move_pct=0.01, asof=ASOF)
    assert e.verdict == "REJECT"
    assert "BELOW" in e.reason


def test_expected_move_above_the_hurdle_passes():
    e = evaluate_buy("RELIANCE", "CE", chain=_chain(),
                     expected_daily_move_pct=5.0, asof=ASOF)
    assert e.verdict == "PASS"


def test_without_an_expectation_it_reports_the_requirement_only():
    e = evaluate_buy("RELIANCE", "CE", chain=_chain(), asof=ASOF)
    assert e.verdict == "REQUIREMENT ONLY"
    assert "must move" in e.reason


def test_shorter_expiry_decays_a_larger_share_of_premium_per_day():
    """The 5-DTE NIFTY case: near expiry, theta eats a far bigger fraction."""
    far = evaluate_buy("X", "CE", chain=_chain(expiry="2026-09-24"), asof=ASOF)
    near = evaluate_buy("X", "CE", chain=_chain(expiry="2026-08-11"), asof=ASOF)
    assert near.theta_pct_of_premium_per_day > far.theta_pct_of_premium_per_day


def test_expiry_day_is_rejected_outright():
    e = evaluate_buy("X", "CE", chain=_chain(expiry="2026-08-06"), asof=ASOF)
    assert e.verdict == "REJECT"
    assert e.dte == 0


def test_call_and_put_breakevens_sit_on_opposite_sides():
    ce = evaluate_buy("X", "CE", chain=_chain(), asof=ASOF)
    pe = evaluate_buy("X", "PE", chain=_chain(), asof=ASOF)
    assert ce.breakeven_underlying > ce.strike
    assert pe.breakeven_underlying < pe.strike


def test_missing_live_premium_refuses_rather_than_guessing():
    bad = _chain()
    for r in bad:
        r["ce_ltp"] = 0
    with pytest.raises(ValueError):
        evaluate_buy("X", "CE", chain=bad, asof=ASOF)


def test_missing_spot_or_expiry_refuses():
    bad = _chain()
    for r in bad:
        r["_spot"] = 0
    with pytest.raises(ValueError):
        evaluate_buy("X", "CE", chain=bad, asof=ASOF)


def test_empty_chain_refuses():
    with pytest.raises(ValueError):
        evaluate_buy("X", "CE", chain=[], asof=ASOF)


def test_bad_option_type_rejected():
    with pytest.raises(ValueError):
        evaluate_buy("X", "XX", chain=_chain(), asof=ASOF)


def test_result_is_serialisable():
    d = evaluate_buy("X", "CE", chain=_chain(), asof=ASOF).to_dict()
    assert d["verdict"] and d["required_daily_move_pct"] > 0
