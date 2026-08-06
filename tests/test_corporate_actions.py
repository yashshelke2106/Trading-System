"""Splits and bonuses must not be read as returns.

NSE bhavcopy carries RAW traded prices, so a 1:2 split reads as -50% and a
1:3 bonus as -67% even though the holder lost nothing. Measured on the
archive: 296 such moves per 400 trading days, including large liquid names
(NMDC 214.45->69.32, MAZDOCK 4729.75->2317.40), so a liquidity filter does not
protect you. Left uncleaned this contaminated the momentum study -- the spread
fell from +5.26%/yr (t=1.01) to +0.98%/yr (t=0.23) once these were removed.

Rights entitlements ("-RE") are a separate trap: temporary instruments that
expire worthless by design.
"""

import numpy as np
import pandas as pd
import pytest

from core.corporate_actions import (clean_panel_returns, clean_returns,
                                    flag_panel, filter_symbols,
                                    is_tradeable_equity_symbol,
                                    looks_like_corporate_action)


@pytest.mark.parametrize("prev,close", [
    (214.45, 69.32),      # NMDC, ~1:3 bonus
    (4729.75, 2317.40),   # MAZDOCK, ~1:2 split
    (1056.10, 477.05),    # BANCOINDIA
])
def test_real_corporate_actions_are_detected(prev, close):
    assert looks_like_corporate_action(prev, close)


@pytest.mark.parametrize("prev,close", [
    (100.0, 105.0),
    (100.0, 92.0),
    (100.0, 72.0),        # a genuine -28% news crash is NOT a corporate action
    (100.0, 118.0),
])
def test_ordinary_moves_are_not_flagged(prev, close):
    assert not looks_like_corporate_action(prev, close)


def test_zero_and_negative_prices_are_not_flagged_as_actions():
    assert not looks_like_corporate_action(0.0, 50.0)
    assert not looks_like_corporate_action(50.0, 0.0)


def test_rights_entitlements_are_not_tradeable_equity():
    for s in ["SICAL-RE", "PRAXIS-RE2", "BTML-RE1", "FOO-W"]:
        assert not is_tradeable_equity_symbol(s), s


def test_ordinary_symbols_are_tradeable():
    for s in ["RELIANCE", "TATASTEEL", "NMDC", "M&M"]:
        assert is_tradeable_equity_symbol(s), s


def test_filter_symbols_drops_only_the_temporary_lines():
    out = filter_symbols(["RELIANCE", "SICAL-RE", "INFY", "BTML-RE1"])
    assert out == ["RELIANCE", "INFY"]


def test_clean_returns_masks_the_action_day_only():
    close = pd.Series([100.0, 102.0, 51.0, 52.0])   # 1:2 split on day 3
    r = clean_returns(close)
    assert np.isnan(r.iloc[2]), "split day must be masked"
    assert r.iloc[1] == pytest.approx(0.02)
    assert r.iloc[3] == pytest.approx(52 / 51 - 1)


def test_clean_returns_uses_prev_close_when_given():
    close = pd.Series([69.32])
    prev = pd.Series([214.45])
    assert np.isnan(clean_returns(close, prev).iloc[0])


def test_flag_panel_marks_the_right_cell():
    close = pd.DataFrame({"A": [100.0, 101.0, 50.5], "B": [50.0, 51.0, 52.0]})
    f = flag_panel(close)
    assert bool(f.loc[2, "A"]) is True
    assert bool(f.loc[2, "B"]) is False


def test_clean_panel_returns_masks_flagged_cells():
    close = pd.DataFrame({"A": [100.0, 101.0, 50.5], "B": [50.0, 51.0, 52.0]})
    r = clean_panel_returns(close)
    assert np.isnan(r.loc[2, "A"])
    assert r.loc[2, "B"] == pytest.approx(52 / 51 - 1)


def test_a_bonus_does_not_survive_as_a_giant_negative_return():
    """The whole point: without cleaning, this single row would rank the name
    as the worst loser in any cross-section."""
    close = pd.Series([214.45, 69.32])
    raw = close.pct_change().iloc[1]
    cleaned = clean_returns(close).iloc[1]
    assert raw < -0.60
    assert np.isnan(cleaned)
