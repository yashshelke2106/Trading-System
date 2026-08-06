"""Statutory charge model — the leg conventions are what matter.

Paper P&L used to be gross: `(exit - entry) * qty` with no STT, exchange fee,
GST, SEBI fee, stamp duty or brokerage. These tests pin the parts that are easy
to get silently wrong (which leg each charge lands on), not the rate values,
which change and are configurable.
"""

import pytest

from core.charges import DEFAULT_RATES, Charges, net_pnl, round_trip


def test_futures_stt_is_sell_leg_only():
    """Equity futures STT is on the sell leg. A higher sell price must
    therefore raise STT; a higher buy price must not."""
    base = round_trip(1000.0, 1000.0, 500, "futures")
    higher_sell = round_trip(1000.0, 1200.0, 500, "futures")
    higher_buy = round_trip(1200.0, 1000.0, 500, "futures")
    assert higher_sell.stt > base.stt
    assert higher_buy.stt == pytest.approx(base.stt)


def test_delivery_stt_hits_both_legs():
    """Unlike futures, delivery STT is charged on buy AND sell."""
    base = round_trip(1000.0, 1000.0, 100, "delivery")
    higher_buy = round_trip(1200.0, 1000.0, 100, "delivery")
    assert higher_buy.stt > base.stt


def test_stamp_duty_is_buy_leg_only():
    base = round_trip(1000.0, 1000.0, 500, "futures")
    higher_buy = round_trip(1200.0, 1000.0, 500, "futures")
    higher_sell = round_trip(1000.0, 1200.0, 500, "futures")
    assert higher_buy.stamp > base.stamp
    assert higher_sell.stamp == pytest.approx(base.stamp)


def test_gst_excludes_stt_and_stamp():
    """GST applies to brokerage + exchange + SEBI only."""
    c = round_trip(1000.0, 1000.0, 500, "futures")
    expected = (c.brokerage + c.exchange + c.sebi) * DEFAULT_RATES["futures"]["gst"]
    assert c.gst == pytest.approx(expected, rel=1e-3)


def test_total_is_the_sum_of_its_parts():
    c = round_trip(1234.5, 1250.0, 300, "futures")
    assert c.total == pytest.approx(
        c.brokerage + c.stt + c.exchange + c.sebi + c.stamp + c.gst, rel=1e-6)


def test_brokerage_takes_the_lower_of_flat_or_pct():
    """Discount-broker rule: Rs 20 per order or 0.03%, whichever is lower.
    Small turnover -> pct wins; large turnover -> the flat cap wins."""
    small = round_trip(100.0, 100.0, 10, "futures")      # 1k turnover/leg
    assert small.brokerage == pytest.approx(2 * (1000.0 * 0.0003), rel=1e-6)
    large = round_trip(10000.0, 10000.0, 500, "futures")  # 50L turnover/leg
    assert large.brokerage == pytest.approx(40.0, rel=1e-6)


def test_delivery_costs_more_than_futures():
    """Documented and measured: delivery round-trip (~0.2%) far exceeds
    futures (~0.05%). If this ever inverts, the schedule is wrong."""
    fut = round_trip(1000.0, 1000.0, 500, "futures")
    del_ = round_trip(1000.0, 1000.0, 500, "delivery")
    assert del_.pct_of_turnover > fut.pct_of_turnover * 3


def test_net_pnl_is_gross_minus_total_charges():
    net, gross, ch = net_pnl(1000.0, 1010.0, 500, "long", "futures")
    assert gross == pytest.approx(5000.0)
    assert net == pytest.approx(gross - ch.total, rel=1e-6)
    assert net < gross


def test_short_swaps_the_charge_legs():
    """A short sells first: entry is the SELL leg. Gross is mirrored, and the
    charges must match the equivalent long because the same two prices are
    still one buy and one sell."""
    long_net, long_gross, long_ch = net_pnl(1000.0, 1010.0, 500, "long", "futures")
    short_net, short_gross, short_ch = net_pnl(1010.0, 1000.0, 500, "short", "futures")
    assert short_gross == pytest.approx(long_gross)
    assert short_ch.total == pytest.approx(long_ch.total)
    assert short_net == pytest.approx(long_net)


def test_a_losing_trade_gets_worse_after_charges():
    net, gross, _ = net_pnl(1000.0, 990.0, 500, "long", "futures")
    assert gross < 0
    assert net < gross


def test_unknown_segment_is_rejected():
    with pytest.raises(ValueError):
        round_trip(100.0, 100.0, 10, "crypto")  # type: ignore[arg-type]


def test_bad_quantity_is_rejected():
    with pytest.raises(ValueError):
        round_trip(100.0, 100.0, 0, "futures")


def test_pct_of_turnover_is_a_fraction_not_a_percent():
    c = round_trip(1000.0, 1000.0, 500, "futures")
    assert 0 < c.pct_of_turnover < 0.01      # futures RT is single-digit bps
    assert isinstance(c, Charges)
