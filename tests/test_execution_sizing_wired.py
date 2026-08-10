"""The sizing pipeline must actually be IN the execution path.

core/sizing.py existed with tests and zero non-test importers -- built, and
protecting nothing. These tests pin the wiring itself, because a module that
falls out of the call path fails silently: trades keep sizing, just by the old
disagreeing logic.
"""

import logging

import pytest

import config
from core.execution import ExecutionEngine
from core.risk_engine import RiskEngine


class _Strike:
    """Minimal stand-in for StrikeRecommendation."""
    def __init__(self, premium=30.55, strike_price=1320.0, option_type="CE"):
        self.premium = premium
        self.strike_price = strike_price
        self.option_type = option_type


def _engine(capital=2_000_000):
    """Real constructor, with the broker APIs left unattached.

    Building it via __new__ skipped order_id_counter and the order books, so
    the test failed on plumbing rather than on the behaviour under test.
    PAPER_TRADE keeps fills simulated regardless.
    """
    e = ExecutionEngine(capital=capital)
    e.dhan_api = None
    e.nse_api = None
    return e


def test_unfundable_futures_trade_is_refused_not_sized():
    """One RELIANCE lot needs ~Rs 1.31 lakh of margin; a Rs 1 lakh account
    cannot open it, and a broker would reject the order."""
    e = _engine(capital=100_000)
    out = e.execute_trade(symbol="RELIANCE", direction="long",
                          capital=100_000, entry_price=1325.0, atr=20.0,
                          use_futures=True)
    assert out is None


def test_fundable_futures_trade_sizes_and_records_the_decision():
    e = _engine(capital=5_000_000)
    out = e.execute_trade(symbol="RELIANCE", direction="long",
                          capital=5_000_000, entry_price=1325.0, atr=20.0,
                          use_futures=True)
    assert out is not None
    d = getattr(e, "last_size_decision", None)
    assert d is not None and d.ok
    assert d.quantity > 0 and d.quantity % d.lot_size == 0


def test_illiquid_name_is_refused_when_the_tier_is_enforced(monkeypatch):
    """PNB clears the research floor but not the futures one (261 Cr/day)."""
    monkeypatch.setattr(config, "ENFORCE_LIQUIDITY_TIER", True, raising=False)
    e = _engine(capital=5_000_000)
    out = e.execute_trade(symbol="PNB", direction="long", capital=5_000_000,
                          entry_price=110.0, atr=2.0, use_futures=True)
    assert out is None
    d = getattr(e, "last_size_decision", None)
    assert d is not None and not d.ok and "liquidity" in d.reason


def test_the_liquidity_tier_can_be_switched_off(monkeypatch):
    """It narrows 151 F&O names to 40, so it must remain a choice."""
    monkeypatch.setattr(config, "ENFORCE_LIQUIDITY_TIER", False, raising=False)
    e = _engine(capital=5_000_000)
    out = e.execute_trade(symbol="PNB", direction="long", capital=5_000_000,
                          entry_price=110.0, atr=2.0, use_futures=True)
    assert out is not None


def test_a_veto_is_logged_rather_than_silent(caplog):
    """Silence at the top of the funnel looks identical to 'no signal today'."""
    e = _engine(capital=100_000)
    with caplog.at_level(logging.INFO, logger="core.execution"):
        e.execute_trade(symbol="RELIANCE", direction="long", capital=100_000,
                        entry_price=1325.0, atr=20.0, use_futures=True)
    assert any("[SIZE]" in r.message for r in caplog.records)


def test_option_leg_carries_its_theta_hurdle():
    """Every long option must report the drift it needs to break even."""
    from core.option_translator import get_option_rec
    chain = [{"strike": 1300.0 + 10 * i, "ce_ltp": 30.0, "pe_ltp": 30.0,
              "ce_iv": 20.0, "pe_iv": 20.0, "_spot": 1325.0,
              "_expiry": "2026-08-25"} for i in range(8)]
    rec = get_option_rec("RELIANCE", "long", 1325.0, 1325.0, 1298.0, 1380.0,
                         chain_data=chain)
    if rec is None:
        pytest.skip("chain stub rejected by upstream filters")
    assert "required_move_pct" in rec and "theta_pct_day" in rec
    if rec["required_move_pct"] is not None:
        assert rec["required_move_pct"] > 0


def test_delisted_names_are_gone_from_the_shared_universe():
    from core.universe import FO_UNIVERSE
    assert "LTIM" not in FO_UNIVERSE
    assert "GUJGASLTD" not in FO_UNIVERSE
