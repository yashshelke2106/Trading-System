"""F&O margin estimation — can the position actually be opened?

Nothing modelled margin before this, so sizing asked only "how many lots fit my
risk budget" and never "can I fund one lot". At the default Rs 1,00,000 capital
most stock futures are unfundable (one lot needs Rs 0.9-1.7 lakh), yet signals
were emitted for them.
"""

import pytest

from core.margin import MarginEstimate, estimate, is_index, max_affordable_lots


def test_long_option_costs_premium_only():
    """The structural fact that matters most: a long option has no SPAN
    margin, because max loss is the premium already paid."""
    m = estimate("RELIANCE", 1325.0, lots=1, instrument="option_long",
                 capital=100000.0, lot_size=500, premium=25.0)
    assert m.span == 0.0
    assert m.exposure == 0.0
    assert m.total == pytest.approx(25.0 * 500 * 1.10)  # premium + broker buffer
    assert m.affordable is True


def test_long_option_requires_a_premium():
    with pytest.raises(ValueError):
        estimate("RELIANCE", 1325.0, instrument="option_long", capital=1e5)


def test_futures_margin_is_a_fraction_of_notional_not_the_notional():
    m = estimate("RELIANCE", 1325.0, lots=1, capital=1e6, lot_size=500)
    assert m.notional == pytest.approx(662500.0)
    # SPAN + exposure + buffer ~ 20% of notional; must be well under notional.
    assert 0.1 * m.notional < m.total < 0.35 * m.notional


def test_futures_margin_far_exceeds_a_long_option():
    """Why a small account can trade options but not stock futures."""
    fut = estimate("RELIANCE", 1325.0, capital=1e5, lot_size=500)
    opt = estimate("RELIANCE", 1325.0, capital=1e5, lot_size=500,
                   instrument="option_long", premium=25.0)
    assert fut.total > opt.total * 5


def test_unfundable_position_is_flagged_with_a_shortfall():
    m = estimate("BAJFINANCE", 1150.70, lots=1, capital=100000.0, lot_size=750)
    assert m.affordable is False
    assert m.shortfall > 0
    assert m.shortfall == pytest.approx(m.total - m.capital)


def test_affordable_position_has_no_shortfall():
    m = estimate("INFY", 1165.0, lots=1, capital=1000000.0, lot_size=400)
    assert m.affordable is True
    assert m.shortfall == 0.0


def test_index_margin_is_lower_than_stock_margin():
    """Index futures carry lower SPAN than single stocks."""
    idx = estimate("NIFTY", 1000.0, capital=1e7, lot_size=100)
    stk = estimate("RELIANCE", 1000.0, capital=1e7, lot_size=100)
    assert idx.total < stk.total
    assert is_index("NIFTY") and not is_index("RELIANCE")


def test_short_option_is_margined_like_futures_not_like_a_long():
    short = estimate("RELIANCE", 1325.0, capital=1e7, lot_size=500,
                     instrument="option_short")
    long_ = estimate("RELIANCE", 1325.0, capital=1e7, lot_size=500,
                     instrument="option_long", premium=25.0)
    assert short.total > long_.total * 5
    assert short.span > 0


def test_max_affordable_lots_returns_zero_when_one_lot_is_too_big():
    assert max_affordable_lots("BAJFINANCE", 1150.70, 100000.0,
                               lot_size=750) == 0


def test_max_affordable_lots_respects_the_capital_fraction_cap():
    """One lot eating 92% of the account is fundable and still not sane."""
    price, lot, cap = 1165.0, 400, 100000.0
    assert max_affordable_lots("INFY", price, cap, lot_size=lot,
                               max_capital_fraction=1.0) >= 1
    assert max_affordable_lots("INFY", price, cap, lot_size=lot,
                               max_capital_fraction=0.5) == 0


def test_more_lots_cost_proportionally_more():
    one = estimate("RELIANCE", 1325.0, lots=1, capital=1e7, lot_size=500)
    three = estimate("RELIANCE", 1325.0, lots=3, capital=1e7, lot_size=500)
    assert three.total == pytest.approx(one.total * 3, rel=1e-6)


def test_bad_inputs_are_rejected():
    with pytest.raises(ValueError):
        estimate("RELIANCE", 0.0, capital=1e5)
    with pytest.raises(ValueError):
        estimate("RELIANCE", 100.0, lots=0, capital=1e5)


def test_estimate_is_serialisable_with_pct_of_capital():
    d = estimate("INFY", 1165.0, capital=100000.0, lot_size=400).to_dict()
    assert isinstance(d, dict)
    assert d["pct_of_capital"] > 0.5      # ~92% of a 1L account
    assert "note" in d


def test_returns_the_dataclass():
    assert isinstance(estimate("INFY", 1165.0, capital=1e6, lot_size=400),
                      MarginEstimate)
